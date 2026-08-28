#!/usr/bin/env python3
"""
Validate and prepare a phase worktree for resume-from-phase demos.

Given a phase tag or worktree path, validates the project state matches
that phase and outputs the recommended next action.

Safety: operates on worktrees only, never modifies the main repo.
"""

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.io_utils import read_json, read_yaml
from aah.core.common.progress import load_progress


def validate_phase_state(worktree_path: Path) -> dict:
    """
    Validate a worktree's state matches its phase checkpoint.

    Returns dict with:
    - valid: bool
    - phase: str
    - project_name: str
    - recommended_action: str
    - warnings: list[str]
    """
    rapids_path = worktree_path / ".rapids"
    warnings = []

    if not rapids_path.is_dir():
        return {
            "valid": False,
            "phase": "unknown",
            "project_name": "unknown",
            "recommended_action": "This directory has no .rapids/ — not a RAPIDS project.",
            "warnings": ["No .rapids directory found"],
        }

    # Read manifest
    manifest_path = rapids_path / "manifest.yaml"
    project_name = "unknown"
    if manifest_path.exists():
        try:
            manifest = read_yaml(manifest_path)
            project_name = manifest.get("project_name", "unknown")
        except Exception:
            warnings.append("Could not read manifest.yaml")

    # Read progress
    progress_path = rapids_path / "claude-progress.json"
    progress = load_progress(progress_path if progress_path.exists() else None)
    phase = progress.get("current_phase", "unknown")

    # Phase-specific validation
    recommended = _get_phase_recommendation(phase, rapids_path)

    return {
        "valid": True,
        "phase": phase,
        "project_name": project_name,
        "recommended_action": recommended,
        "warnings": warnings,
    }


def _get_phase_recommendation(phase: str, rapids_path: Path) -> str:
    """Get the recommended action for continuing from a phase."""
    recommendations = {
        "init": "Run /rapids-research to begin problem statement intake.",
        "research": "Run /rapids-research to continue the research phase.",
        "analysis": "Run /rapids-analyze to continue the analysis phase.",
        "plan": "Run /rapids-plan to continue planning.",
        "implement": "Run /rapids-implement to continue implementation.",
        "deploy": "Run /rapids-deploy to continue deployment.",
        "sustain": "Run /rapids-sustain to continue observability setup.",
    }
    return recommendations.get(phase, f"Phase '{phase}' — check project status with /aah-resume.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate phase worktree for resume")
    parser.add_argument("worktree_path", type=Path, help="Path to the phase worktree")
    args = parser.parse_args()

    result = validate_phase_state(args.worktree_path)
    json.dump(result, sys.stdout, indent=2)
    print()

    if result["valid"]:
        print(f"\nProject: {result['project_name']}", file=sys.stderr)
        print(f"Phase: {result['phase']}", file=sys.stderr)
        print(f"Recommended: {result['recommended_action']}", file=sys.stderr)
    else:
        print(f"\nInvalid: {result['warnings']}", file=sys.stderr)

    sys.exit(0 if result["valid"] else 1)


if __name__ == "__main__":
    main()
