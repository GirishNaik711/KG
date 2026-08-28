#!/usr/bin/env python3
"""Port-artifact completeness gate (design §9).

Additive, keyed on a core node. For every port activity bound to `<node>` that
FIRED (i.e. was expected to run), verify it actually ran and produced its
declared `produces`. Distinguishes:

  · correctly skipped   — status: skipped / deferred-stub → no check (fine)
  · should-have-run     — status: pending / failed, or produces missing → FAIL

Fully optional: no `.aah/port-registry.yaml` → no checks → identical legacy
behavior. Existing core-artifact gates are untouched — call this alongside them.

Usage (per-phase gate wiring):
    from aah.core.gates.validate_port_artifacts import validate_port_artifacts
    passed, issues = validate_port_artifacts(project_path, node="architecture")

CLI (manual / hook):
    aah run core.gates.validate_port_artifacts --node architecture
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.io_utils import read_yaml


# Statuses that mean "this activity was correctly NOT run" — skip the check.
_OK_TO_SKIP = {"skipped", "deferred-stub"}
# Statuses that mean "this activity finished successfully".
_DONE = {"completed"}


def _registry_path(project_path: Path) -> Path:
    return project_path / ".aah" / "port-registry.yaml"


def _produce_exists(project_path: Path, produced: str) -> bool:
    """A produces path may be given relative to the project root or .aah/."""
    p = str(produced)
    return ((project_path / p).exists() or (project_path / ".aah" / p).exists())


def validate_port_artifacts(
    project_path: Path,
    node: str | None = None,
) -> tuple[bool, list[str]]:
    """Validate that fired port activities produced their artifacts.

    Args:
        project_path: the project root (folder holding .aah/).
        node: restrict to activities bound to this core node. None → all nodes.

    Returns (passed, issues). No registry → (True, []).
    """
    reg_path = _registry_path(project_path)
    if not reg_path.exists():
        return True, []  # ports optional — nothing to check

    try:
        registry = read_yaml(reg_path)
    except Exception as e:
        return False, [f"port-registry.yaml cannot be parsed: {e}"]

    issues: list[str] = []

    for a in registry.get("activities", []) or []:
        if node is not None and a.get("target") != node:
            continue

        status = a.get("status", "pending")
        aid = a.get("id", "UNKNOWN")

        # Correctly-not-run activities are fine.
        if status in _OK_TO_SKIP:
            continue

        # Fired-but-not-finished → the coordinate ran but the activity stalled.
        if status not in _DONE:
            issues.append(
                f"{aid} (target={a.get('target')}, position={a.get('position')}): "
                f"status is '{status}' — fired but not completed"
            )
            continue

        # Completed → every declared `produces` must exist.
        for produced in a.get("produces", []) or []:
            if not _produce_exists(project_path, produced):
                issues.append(
                    f"{aid}: marked completed but produced artifact missing: {produced}"
                )

    return (not issues), issues


def main() -> None:
    parser = argparse.ArgumentParser(description="Port-artifact completeness gate")
    parser.add_argument("--node", help="Restrict to one core node (default: all)")
    parser.add_argument("--project-path", type=Path)
    # Accept hook-style stdin too, but CLI flags take precedence.
    args, _ = parser.parse_known_args()

    from aah.core.common.config import resolve_project_path

    project_path = None
    if args.project_path is not None:
        project_path = resolve_project_path(args.project_path)
    if project_path is None:
        # Try hook input (cwd), then cwd walk-up.
        try:
            hook_input = json.load(sys.stdin)
        except (json.JSONDecodeError, EOFError):
            hook_input = {}
        cwd = hook_input.get("cwd")
        project_path = resolve_project_path(Path(cwd) if cwd else None)

    if project_path is None:
        sys.exit(0)  # no project — nothing to gate

    passed, issues = validate_port_artifacts(project_path, node=args.node)

    if not passed:
        print("Port artifact validation FAILED:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
