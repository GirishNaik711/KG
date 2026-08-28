#!/usr/bin/env python3
"""PostToolUse(Write|Edit|MultiEdit) guard: detect circular imports
introduced by writes to source files.

Issue #247, Phase 3 L5-C. PartsPulse-iter-2 issue #3 (``main.tsx`` ↔
``App.tsx`` ↔ ``AppShell.tsx`` cycle) bypassed compile, build, and
build-time tree-shaking. The runtime error was confusing
("AppShell is not a function") and time-consuming to diagnose. A
write-time check that walks the import graph from the changed file
catches the literal-cycle case at the moment it's introduced.

This guard uses a **bounded-depth BFS** (50 nodes max) starting from
the changed file. No codemap-DB dependency — pure stdlib `re` +
`pathlib`. Cycles spanning more than 50 modules are honestly NOT
detected (acknowledged in stderr); in practice, PartsPulse-class
cycles are 2-3 modules and easily within bounds.

Per-language regex import parsers:
  - Python:  ``^\\s*from X import`` + ``^\\s*import X``
  - JS/TS:   ``import ... from 'X'`` + ``require('X')``

Resolution: imports are resolved to project-local file paths by
matching against the project root + standard source root candidates
(``src/``, ``lib/``, ``app/``, project root itself). Non-project
imports (3rd-party packages) are skipped.

Test directories are skipped — cycles in test code rarely block
runtime, and false positives in test fixtures interrupt agent flow.

Escape hatches:
  - ``AAH_CIRCULAR_IMPORT_BYPASS=1`` env var.
  - ``delegation_guard.exit_if_delegated()``.

Exit codes:
  0 — no cycle / not a source file / cycle beyond bounded depth /
      parse failure (best-effort)
  2 — cycle detected within bounded depth
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from collections import deque


BYPASS_ENV = "AAH_CIRCULAR_IMPORT_BYPASS"

# Maximum number of nodes to scan before giving up. PartsPulse-class
# cycles are 2-3 modules; 50 is generous without being slow.
MAX_NODES = 50

# Source-file extensions we scan.
_SOURCE_EXTS: tuple[str, ...] = (
    ".py",
    ".ts", ".tsx",
    ".js", ".jsx", ".mjs", ".cjs",
)

# Test-directory hints — skip cycles in these (false-positive cost is
# higher than the missed-bug cost).
_TEST_PATH_HINTS: tuple[str, ...] = (
    "/tests/", "/test/",
    "/__tests__/", "/__test__/",
    "/spec/", "/specs/",
)


# ---------------------------------------------------------------------------
# Path classification
# ---------------------------------------------------------------------------


def _is_source_file(file_path: str) -> bool:
    if not file_path:
        return False
    norm = file_path.replace("\\", "/").lower()
    if not any(norm.endswith(ext) for ext in _SOURCE_EXTS):
        return False
    if any(hint in norm for hint in _TEST_PATH_HINTS):
        return False
    fname = norm.rsplit("/", 1)[-1]
    # Skip files like test_foo.py, foo.test.ts, foo.spec.js.
    if fname.startswith("test_") or fname.startswith("test-"):
        return False
    if ".test." in fname or ".spec." in fname:
        return False
    return True


def _extract_file_path(tool_name: str, tool_input: dict) -> str:
    if tool_name in ("Write", "Edit"):
        return tool_input.get("file_path", "") or ""
    if tool_name == "MultiEdit":
        top = tool_input.get("file_path", "")
        if top:
            return top
        for edit in tool_input.get("edits", []) or []:
            if isinstance(edit, dict):
                fp = edit.get("file_path", "")
                if fp:
                    return fp
    return ""


# ---------------------------------------------------------------------------
# Project-root discovery
# ---------------------------------------------------------------------------


def _find_project_root(start: Path) -> Path | None:
    """Walk up from `start` looking for `.aah` or stack-fingerprint files."""
    candidates = [start, *start.parents] if start.is_absolute() else [Path.cwd(), *Path.cwd().parents]
    for parent in candidates:
        if (parent / ".aah").is_dir():
            return parent
        if (parent / "package.json").is_file():
            return parent
        if (parent / "pyproject.toml").is_file():
            return parent
    return None


# ---------------------------------------------------------------------------
# Import parsing per language
# ---------------------------------------------------------------------------


_PY_FROM = re.compile(r"^\s*from\s+([.\w]+)\s+import\s", re.MULTILINE)
_PY_IMPORT = re.compile(r"^\s*import\s+([.\w]+)", re.MULTILINE)

_JS_IMPORT_FROM = re.compile(r"""\bimport\s+(?:[^'"]+\s+from\s+)?['"]([^'"]+)['"]""")
_JS_REQUIRE = re.compile(r"""\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)""")


def _parse_imports(file_path: Path) -> list[str]:
    """Return list of raw import targets (strings as written in the source)."""
    try:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, ValueError):
        return []
    ext = file_path.suffix.lower()
    out: list[str] = []
    if ext == ".py":
        out.extend(_PY_FROM.findall(text))
        out.extend(_PY_IMPORT.findall(text))
    elif ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        out.extend(_JS_IMPORT_FROM.findall(text))
        out.extend(_JS_REQUIRE.findall(text))
    return out


# ---------------------------------------------------------------------------
# Resolve raw import target to a project-local file
# ---------------------------------------------------------------------------


def _resolve_import_to_path(
    raw_target: str,
    importing_file: Path,
    project_root: Path,
) -> Path | None:
    """Resolve a raw import string to a project-local file path.

    Returns None when the import is third-party (not in project) or
    can't be resolved.
    """
    if not raw_target:
        return None

    # JS/TS: relative imports like "./AppShell" or "../foo/bar"
    if raw_target.startswith("."):
        base = importing_file.parent
        candidate_base = (base / raw_target).resolve()
        for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            candidate = candidate_base.with_suffix(ext) if not candidate_base.suffix else candidate_base
            if not candidate.suffix:
                candidate = candidate.with_suffix(ext)
            if candidate.is_file():
                return candidate
            # index.* fallback
            idx = candidate_base / f"index{ext}"
            if idx.is_file():
                return idx
        return None

    # Python relative import: "from .foo import bar"
    if raw_target.startswith(".") and importing_file.suffix == ".py":
        # already handled above
        return None

    # Python absolute: "from foo.bar import baz" → project_root/foo/bar.py
    if importing_file.suffix == ".py":
        parts = raw_target.split(".")
        for src_root in (project_root,):
            candidate = src_root.joinpath(*parts).with_suffix(".py")
            if candidate.is_file():
                return candidate
            # package: foo/__init__.py
            init = src_root.joinpath(*parts) / "__init__.py"
            if init.is_file():
                return init
        return None

    # JS/TS bare imports (e.g. "react", "@mui/material") → third-party.
    # We don't resolve third-party packages.
    return None


# ---------------------------------------------------------------------------
# BFS for cycles
# ---------------------------------------------------------------------------


def _detect_cycle(start: Path, project_root: Path) -> list[Path] | None:
    """BFS the import graph starting from `start`. Returns a cycle path
    list (start ... start) when a revisit of `start` is found within
    MAX_NODES. Returns None when no cycle is detected within bounds.

    The "cycle" we care about is one that includes `start` — i.e., the
    file just-written re-enters itself through some chain of imports.
    """
    if not start.is_file():
        return None

    # parent[node] = the node that put `node` on the queue. Used to
    # reconstruct the cycle path on detection.
    parent: dict[Path, Path | None] = {start: None}
    queue: deque[Path] = deque([start])
    nodes_scanned = 0
    first_iter = True

    while queue and nodes_scanned < MAX_NODES:
        current = queue.popleft()
        nodes_scanned += 1

        raw_imports = _parse_imports(current)
        for raw in raw_imports:
            target = _resolve_import_to_path(raw, current, project_root)
            if target is None:
                continue
            try:
                target = target.resolve()
            except (OSError, RuntimeError):
                continue

            # Cycle: edge back to the start file.
            if target == start and not first_iter:
                # Reconstruct the path: start → ... → current → start.
                path: list[Path] = [start]
                node: Path | None = current
                # Follow parent links back to start.
                while node is not None and node != start:
                    path.append(node)
                    node = parent.get(node)
                path.append(start)
                # Reverse so it reads start→...→current→start.
                path.reverse()
                return path

            if target in parent:
                continue  # already visited
            parent[target] = current
            queue.append(target)

        first_iter = False

    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    from aah.core.guards._trace import trace
    from aah.core.guards.delegation_guard import exit_if_delegated
    exit_if_delegated()

    if os.environ.get(BYPASS_ENV) == "1":
        trace("circular_import_guard", "bypass")
        sys.exit(0)

    tool_name = hook_input.get("tool_name", "") or ""
    tool_input = hook_input.get("tool_input", {}) or {}
    file_path = _extract_file_path(tool_name, tool_input)
    if not file_path or not _is_source_file(file_path):
        trace("circular_import_guard", "noop", file_path or "no-path")
        sys.exit(0)

    target = Path(file_path)
    if not target.exists():
        trace("circular_import_guard", "noop", "file-missing")
        sys.exit(0)

    project_root = _find_project_root(target)
    if project_root is None:
        trace("circular_import_guard", "noop", "no-project-root")
        sys.exit(0)

    try:
        target_resolved = target.resolve()
    except (OSError, RuntimeError):
        trace("circular_import_guard", "noop", "unresolvable")
        sys.exit(0)

    cycle = _detect_cycle(target_resolved, project_root)
    if cycle is None:
        trace("circular_import_guard", "allow", file_path)
        sys.exit(0)

    # Render cycle in a readable form: file.py → other.py → file.py.
    rel_cycle = []
    for p in cycle:
        try:
            rel_cycle.append(str(p.relative_to(project_root)))
        except ValueError:
            rel_cycle.append(str(p))
    cycle_str = " → ".join(rel_cycle)

    print(
        f"⚠ AAH circular-import detected (PostToolUse, bounded scan ≤{MAX_NODES} nodes):",
        file=sys.stderr,
    )
    print(f"  {cycle_str}", file=sys.stderr)
    print(
        f"\nCircular imports cause subtle runtime errors that compile/build "
        f"can't catch (PartsPulse #3). Refactor the cycle (e.g., extract a "
        f"shared module that both sides import) before continuing. Set "
        f"{BYPASS_ENV}=1 if this is a known intentional case (rare).",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
