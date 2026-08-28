"""Language adapter registry (Phase 3 L2 / issue #247).

Single entry point for per-language commands across the implement
phase. Adding a new language:

    1. Create lang_checks/<lang>.py with a class subclassing
       LanguageAdapter (see base.py for the contract).
    2. Insert the class into the ``_ADAPTERS`` tuple below, BEFORE
       GenericAdapter (which is the always-true fallback).
    3. Add a fixture project under scripts/tests/fixtures/lang_<lang>/
       and a per-adapter test in test_lang_checks_<lang>.py.

Public API:
    Cmd               — frozen dataclass for resolved commands.
    LanguageAdapter   — ABC base class for adapter subclasses.
    detect(project_path, manifest=None) -> LanguageAdapter
                      — return the right adapter for the project.
                        Never raises. Always returns at least
                        GenericAdapter for unknown stacks.
    discover_package_roots(project_path) -> list[(root, adapter_name)]
                      — every package root in the tree, each with its
                        OWN adapter. ``detect()`` answers "one adapter
                        for the project", which is wrong for the
                        standard fullstack layout; this answers "which
                        packages are there".
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from aah.core.common.io_utils import read_yaml
from aah.core.build.lang_checks._helpers import (
    manifest_stack_field,
    rewrite_bare_venv_command,
)
from aah.core.build.lang_checks.base import Cmd, LanguageAdapter
from aah.core.build.lang_checks.generic import GenericAdapter
from aah.core.build.lang_checks.go import GoAdapter
from aah.core.build.lang_checks.java import JavaAdapter
from aah.core.build.lang_checks.node import NodeAdapter
from aah.core.build.lang_checks.python import PythonAdapter
from aah.core.build.lang_checks.rust import RustAdapter


# Adapter precedence: more-specific first, generic fallback last.
# Within a language family, sub-frameworks (e.g. "fastapi" inside
# "python-fastapi") are handled by each adapter's own detect() —
# the registry just walks in order and takes the first match.
#
# Order rationale (most-disambiguated first):
#   - Python and Node both have multiple file fingerprints; they're
#     listed first because their stack-string overlap with each other
#     is minimal in practice.
#   - Go is third because its stack token "go" is short and its
#     detect() needs careful disambiguation against other tokens
#     containing "go" (e.g. django).
#   - Rust and Java are listed before Generic in alphabetical order
#     for stability; they don't conflict with Go/Node/Python.
#   - GenericAdapter MUST be last — it always claims if reached.
_ADAPTERS: tuple[type[LanguageAdapter], ...] = (
    PythonAdapter,
    NodeAdapter,
    GoAdapter,
    RustAdapter,
    JavaAdapter,
    GenericAdapter,
)


def detect(
    project_path: Path,
    manifest: dict[str, Any] | None = None,
) -> LanguageAdapter:
    """Return the right adapter for the project. Never raises.

    Args:
        project_path: project root.
        manifest: optional pre-loaded manifest dict. If None, loads from
            ``project_path / ".aah" / "manifest.yaml"``. Pass
            explicitly when caller already has it loaded to avoid a
            duplicate read.

    Returns:
        A concrete LanguageAdapter subclass instance. Always non-None
        because GenericAdapter.detect() always returns True.
    """
    if manifest is None:
        manifest_path = project_path / ".aah" / "manifest.yaml"
        if manifest_path.exists():
            try:
                manifest = read_yaml(manifest_path)
            except Exception:
                manifest = {}
        else:
            manifest = {}

    for adapter_cls in _ADAPTERS:
        if adapter_cls.detect(project_path, manifest):
            return adapter_cls(project_path=project_path, manifest=manifest)

    # Defensive: GenericAdapter.detect always returns True so this is
    # unreachable, but if a future maintainer changes Generic's detect
    # we still want a deterministic answer.
    return GenericAdapter(project_path=project_path, manifest=manifest)


# Which manifest files mark a directory as a package root of which adapter,
# in _ADAPTERS precedence order (so a directory carrying two families
# resolves the same way detect() would).
#
# This is a per-directory map rather than a detect() call because the
# adapters deliberately glob one level down: PythonAdapter.detect(root)
# matches `root/api/pyproject.toml`, so asking detect() about a node root
# with a Python subpackage answers "python" and the node package is lost —
# the exact class of miss this discovery exists to close. A package root
# must earn its adapter from its OWN files.
_FINGERPRINT_ADAPTERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("python", ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile")),
    ("node", ("package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml")),
    ("go", ("go.mod",)),
    ("rust", ("Cargo.toml",)),
    ("java", ("pom.xml", "build.gradle", "build.gradle.kts")),
)

# Files that mark a directory as a language package root — the canonical
# list, so lint-config placement and the quality gate cannot drift apart.
PACKAGE_FINGERPRINTS = frozenset(
    name for _adapter, names in _FINGERPRINT_ADAPTERS for name in names
)

# Directories that never hold a first-party package root. Descending into
# node_modules or .venv would "discover" thousands of vendored packages and
# lint dependencies as if they were project code.
_DISCOVERY_SKIP_DIRS = frozenset({
    "node_modules", "vendor", "site-packages", ".venv", "venv", "env",
    "dist", "build", "target", "out", "__pycache__", "coverage", "htmlcov",
})


def discover_package_roots(
    project_path: Path, max_depth: int = 2
) -> list[tuple[Path, str]]:
    """Return every package root in the tree with its own adapter name.

    ``detect()`` resolves ONE adapter for a whole project, which silently
    ignores a second package: on ``backend/pyproject.toml`` +
    ``frontend/package.json`` it returns Python and the frontend is never
    linted. This walks the tree instead, so each package earns its own
    adapter from its own manifest files.

    Depth 2 covers ``backend/`` + ``frontend/`` (depth 1) and monorepo
    layouts like ``apps/web/`` + ``packages/api/`` (depth 2).

    A candidate whose accepted ancestor already has the same adapter is
    dropped — an npm workspace root's lint command already covers its
    members. A different language under an accepted root is kept.

    Returns ``[]`` for an empty or nonexistent tree; callers decide what
    that means.
    """
    root = Path(project_path).resolve()
    if not root.is_dir():
        return []

    accepted: list[tuple[Path, str]] = []

    def _walk(directory: Path, depth: int) -> None:
        adapter_name = next(
            (
                name
                for name, fingerprints in _FINGERPRINT_ADAPTERS
                if any((directory / f).exists() for f in fingerprints)
            ),
            None,
        )
        if adapter_name is not None:
            shadowed = any(
                name == adapter_name and directory.is_relative_to(ancestor)
                for ancestor, name in accepted
            )
            if not shadowed:
                accepted.append((directory, adapter_name))
        if depth >= max_depth:
            return
        try:
            children = sorted(directory.iterdir())
        except OSError:
            return
        for child in children:
            if child.is_symlink() or not child.is_dir():
                continue
            if child.name.startswith(".") or child.name in _DISCOVERY_SKIP_DIRS:
                continue
            _walk(child, depth + 1)

    _walk(root, 0)
    return sorted(accepted, key=lambda item: item[0].as_posix())


def resolve_test_command(
    project_path: Path,
    feature_filter: str | None = None,
    manifest: dict[str, Any] | None = None,
) -> Cmd | None:
    """Resolve the manifest override or the detected adapter's test command."""
    adapter = detect(project_path, manifest)
    override = manifest_stack_field(adapter.manifest, "test_command")
    if override:
        override = rewrite_bare_venv_command(override)
        try:
            argv = shlex.split(override)
        except ValueError:
            return None
        if not argv:
            return None
        return Cmd(
            argv=argv,
            cwd=project_path,
            timeout_sec=300,
            label="test (from manifest)",
        )
    return adapter.test_command(feature_filter=feature_filter)


__all__ = [
    "Cmd",
    "LanguageAdapter",
    "PACKAGE_FINGERPRINTS",
    "detect",
    "discover_package_roots",
    "resolve_test_command",
]
