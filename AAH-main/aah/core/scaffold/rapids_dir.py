#!/usr/bin/env python3
"""Scaffold the canonical .aah/ directory structure for a project."""

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.io_utils import ensure_dir


# The canonical .aah/ directory structure
AAH_DIRS = [
    "discuss",
    "architecture",
    "plan",
    "plan/features",
    "plan/sprint-contracts",
    "build",
    "build/test-results",
    "build/quality-results",
    "deploy",
    "deploy/infra",
    "brownfield",
    "audit",
    "iterations",
    "security/semgrep-rules",
    "security/nuclei-templates",
]

# Backwards-compatible import name; remove with other RAPIDS aliases at 1.0.
RAPIDS_DIRS = AAH_DIRS


def create_rapids_dir(project_path: Path) -> Path:
    """
    Create the full .aah/ directory structure under a project.

    Returns the path to the .aah/ directory.
    """
    aah_path = project_path / ".aah"

    if aah_path.exists():
        print(f"Warning: .aah/ already exists at {aah_path}", file=sys.stderr)

    # Create all directories
    for d in AAH_DIRS:
        ensure_dir(aah_path / d)

    # Migrate legacy security state and seed curated rules (non-clobbering).
    from aah.core.security.state import ensure_security_state

    ensure_security_state(project_path, notify=True)

    return aah_path


def verify_rapids_dir(project_path: Path) -> list[str]:
    """Verify that the .aah/ directory structure is complete. Returns missing dirs."""
    aah_path = project_path / ".aah"
    missing = []
    for d in AAH_DIRS:
        dir_path = aah_path / d
        if not dir_path.is_dir():
            missing.append(d)
    return missing


def main() -> None:
    parser = argparse.ArgumentParser(description="AAH directory scaffolder")
    sub = parser.add_subparsers(dest="command", required=True)

    create_p = sub.add_parser("create", help="Create .aah/ directory structure")
    create_p.add_argument("--project-path", type=Path, default=Path.cwd())

    verify_p = sub.add_parser("verify", help="Verify .aah/ directory structure")
    verify_p.add_argument("--project-path", type=Path, default=Path.cwd())

    args = parser.parse_args()

    if args.command == "create":
        aah_path = create_rapids_dir(args.project_path)
        result = {
            "aah_dir": str(aah_path),
            "rapids_dir": str(aah_path),  # pre-1.0 compatibility alias
            "directories": AAH_DIRS,
            "status": "created",
        }
        json.dump(result, sys.stdout, indent=2)
        print()
        print(f".aah/ created at {aah_path}", file=sys.stderr)

    elif args.command == "verify":
        missing = verify_rapids_dir(args.project_path)
        if missing:
            json.dump({"valid": False, "missing": missing}, sys.stdout, indent=2)
            print()
            sys.exit(1)
        json.dump({"valid": True, "missing": []}, sys.stdout, indent=2)
        print()

    sys.exit(0)


if __name__ == "__main__":
    main()
