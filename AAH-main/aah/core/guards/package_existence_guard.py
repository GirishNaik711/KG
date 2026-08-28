#!/usr/bin/env python3
"""PreToolUse(Bash) guard: BLOCK batch installs of names absent from the
committed lockfile.

A hallucinated or slopsquatted
package name (`reqeusts`, a typo-domain) is silently installed today — there
is no existence/provenance gate. The sibling ``dependency_policy_guard``
checks *version* policy, not whether the name is real.

This gate is DETERMINISTIC and OFFLINE. The committed ``uv.lock`` is the source
of truth: a human ran ``uv lock`` and reviewed the diff, so names in the lock
are implicitly vetted. A batch install whose declared names all resolve in the
committed lockfile passes; a name absent from the lock fails. The requirements
file being installed is NOT treated as its own lock (that would resolve a
hallucinated name against itself). There is NO live registry lookup, NO agent
adjudication, and NO network
call (which would reintroduce the SSRF surface this design exists to avoid —
see security/pre_check.py:_fetch_json).

What it intercepts (spike Q1 — gate batch installs, exempt intentional adds):
  - ``pip install -r <reqfile>`` / ``uv pip install -r <reqfile>`` — the
    reqfile is agent-authored and is NOT the lockfile, so a hallucinated name
    there is the real vector. Each declared name must resolve in the lock.
  - ``npm ci`` / ``uv sync`` / ``poetry install`` (no package args) —
    lockfile-native installs; they cannot introduce an unresolved name, so
    they pass trivially.
  - ``pip install X`` / ``uv add X`` / ``npm install X`` (bare name) — the
    intentional-add path. Passed through; that is dependency_policy_guard's
    job, not this guard's. Blocking it would false-block every new dependency.

Snapshot-date defense (spike Q2): DROPPED from v1. "Was this name on the
registry as of date D" needs registry metadata = a network lookup, which the
non-negotiable no-network constraint forbids. The committed lockfile is itself
the reviewed temporal snapshot.

Escape hatch:
  - ``RAPIDS_PACKAGE_EXISTENCE_BYPASS=1`` env var.

Exit codes:
  0 — allowed (not a batch install / no lockfile / all names resolve / bypass)
  2 — blocked (a declared name is absent from the committed lockfile)
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from aah.core.guards.dependency_policy_guard import _find_project_root


BYPASS_ENV = "RAPIDS_PACKAGE_EXISTENCE_BYPASS"


# ``pip install -r <file>`` / ``uv pip install -r <file>`` — the captured group
# is the requirements-file path. This is the only pattern with teeth.
_REQFILE_PATTERN = re.compile(
    r"\b(?:uv\s+)?pip\s+install\b[^|;&]*?\s-r\s+([^\s|;&<>]+)",
    re.IGNORECASE,
)

# Lockfile-native installs (no package args) — pass trivially, they cannot
# introduce a name not already resolved in the lock.
_LOCKFILE_NATIVE_PATTERN = re.compile(
    r"\b(npm\s+ci|uv\s+sync|poetry\s+install|pnpm\s+install\s+--frozen-lockfile)\b",
    re.IGNORECASE,
)


def _normalize_pip(name: str) -> str:
    """PEP 503 normalization: lowercase, runs of -/_/. collapse to a single -."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _reqfile_names(path: Path) -> list[str]:
    """Extract declared package names from a requirements file.

    Reuses the split shape from security/pre_check.py:check_requirements.
    Missing/unreadable file → [] (fail-open; not a hallucinated-name signal).
    """
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []
    names = []
    for line in text.splitlines():
        line = line.strip()
        # Skip blanks, comments, and flag/option lines (-r, -e, --hash=...).
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        name = re.split(r"[=<>!~\[;]", line)[0].strip()
        if name:
            names.append(name)
    return names


def _committed_lockfile_names(project_root: Path) -> set[str] | None:
    """Return the set of package names resolved in the committed uv.lock,
    PEP 503-normalized. None when no lockfile is present (guard no-ops → allow).

    uv.lock is the source of truth — the reviewed resolution. We do NOT treat
    the requirements file being installed as its own lock (that would resolve
    a hallucinated name against itself). Names are extracted by regex; tomllib
    is not safe to assume on the 3.10 test env and a name-set needs no parser.
    """
    uv_lock = project_root / "uv.lock"
    if not uv_lock.exists():
        return None
    try:
        text = uv_lock.read_text(encoding="utf-8")
    except Exception:
        return None
    return {_normalize_pip(m) for m in re.findall(r'(?m)^name = "([^"]+)"', text)}


def _batch_install_targets(command: str) -> list[tuple[str, str]]:
    """Classify the command into gated batch-install targets.

    Returns:
      [("pip", "<reqfile>")]      — pip install -r, needs resolution check
      [("lockfile_native", "")]   — npm ci / uv sync etc., trivial pass
      []                          — bare add / non-install, no-op
    """
    m = _REQFILE_PATTERN.search(command)
    if m:
        return [("pip", m.group(1))]
    if _LOCKFILE_NATIVE_PATTERN.search(command):
        return [("lockfile_native", "")]
    return []


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    from aah.core.guards._trace import trace
    from aah.core.guards.delegation_guard import exit_if_delegated
    exit_if_delegated()

    if os.environ.get(BYPASS_ENV) == "1":
        trace("package_existence_guard", "bypass")
        sys.exit(0)

    tool_name = hook_input.get("tool_name", "") or ""
    if tool_name != "Bash":
        trace("package_existence_guard", "noop", "non-bash")
        sys.exit(0)

    command = (hook_input.get("tool_input", {}) or {}).get("command", "") or ""
    if not command:
        trace("package_existence_guard", "noop", "empty-command")
        sys.exit(0)

    targets = _batch_install_targets(command)
    if not targets:
        trace("package_existence_guard", "noop", "not-batch-install")
        sys.exit(0)

    ecosystem, target = targets[0]
    if ecosystem == "lockfile_native":
        # Lockfile-native install — cannot introduce an unresolved name.
        trace("package_existence_guard", "allow", "lockfile-native")
        sys.exit(0)

    project_root = _find_project_root()
    if project_root is None:
        trace("package_existence_guard", "noop", "no-project-root")
        sys.exit(0)

    lock_names = _committed_lockfile_names(project_root)
    if lock_names is None:
        # No committed lockfile → fresh project; nothing to resolve against.
        trace("package_existence_guard", "noop", "no-lockfile")
        sys.exit(0)

    reqfile = Path(target)
    if not reqfile.is_absolute():
        reqfile = project_root / target
    declared = _reqfile_names(reqfile)
    if not declared:
        # Missing/empty reqfile — fail-open on infra ambiguity, not a
        # hallucinated-name signal.
        # ponytail: fail-open here; a reqfile the guard can't read is an infra
        # gap, not a slopsquat. Upgrade path: block on unreadable-but-present
        # reqfile if that turns out to be a real vector.
        trace("package_existence_guard", "noop", "empty-reqfile")
        sys.exit(0)

    unresolved = [n for n in declared if _normalize_pip(n) not in lock_names]
    if not unresolved:
        trace("package_existence_guard", "allow", f"{len(declared)} resolved")
        sys.exit(0)

    print(
        "⚠ RAPIDS package-existence gate — install blocked:",
        file=sys.stderr,
    )
    for n in unresolved:
        print(
            f"  {n} — declared in {target} but absent from the committed lockfile",
            file=sys.stderr,
        )
    print(
        f"\nThese names do not resolve in the project's committed lockfile "
        f"(uv.lock). A name absent from the reviewed lock "
        f"may be a hallucinated or slopsquatted package. Add the dependency the "
        f"sanctioned way (e.g. `uv add <name>` / `pip install <name>`) so it is "
        f"reviewed into the lockfile, or set {BYPASS_ENV}=1 for a one-off bypass.",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
