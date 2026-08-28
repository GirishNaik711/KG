#!/usr/bin/env python3
"""Centralized phase transition handler.

Single entry point for updating manifest.yaml and claude-progress.json
at the START and END of each AAH phase.

Usage:
    aah run core.common.phase_transition start discuss
    aah run core.common.phase_transition end discuss
"""

import argparse
import sys
from pathlib import Path

from aah.core.common.manifest import update_phase, find_manifest
from aah.core.common.progress import update_progress, find_progress
from aah.core.common.git_utils import push_to_remote


PHASE_ORDER = ["init", "discuss", "architecture", "plan", "build", "deploy", "complete"]

PHASE_START_MESSAGES = {
    "discuss": "Executing discuss activities",
    "architecture": "Executing architecture activities",
    "plan": "Building feature specs, DAG, and sprint contracts",
    "build": "Beginning build phase",
    "deploy": "Generating deployment artifacts",
}

PHASE_END_MESSAGES = {
    "discuss": "Discuss phase complete. Ready for architecture.",
    "architecture": "Architecture phase complete. Ready for plan.",
    "plan": "Plan phase complete. Ready for implementation.",
    "build": "All waves complete. Ready for deploy.",
    "deploy": "Deploy phase complete. Project delivery finished.",
}


def transition_start(phase: str) -> None:
    """Handle phase START: update manifest + progress."""
    manifest_path = find_manifest()
    if manifest_path is None:
        print("Error: manifest.yaml not found", file=sys.stderr)
        sys.exit(1)

    # update_phase() already syncs both manifest.yaml AND claude-progress.json
    update_phase(manifest_path, phase)

    # Also set next_steps in progress
    progress_path = find_progress()
    if progress_path:
        msg = PHASE_START_MESSAGES.get(phase, f"Executing {phase} phase")
        update_progress(progress_path, next_steps=msg)

    print(f"Phase transition START → '{phase}'", file=sys.stderr)


def _next_phase(current: str) -> str | None:
    """Return the next phase in sequence, or None if at the end."""
    try:
        idx = PHASE_ORDER.index(current)
        if idx + 1 < len(PHASE_ORDER):
            return PHASE_ORDER[idx + 1]
    except ValueError:
        pass
    return None


def _auto_resolve_standards(manifest_path: Path) -> None:
    """Auto-resolve and embed enterprise standards at end of plan phase.

    Flow:
      1. Auto-extract rules from text-based unstructured docs (no user action needed)
      2. Resolve all sources (structured YAML + extraction cache + industry defaults)
      3. Embed lightweight IDs into feature YAMLs
    """
    try:
        from aah.core.common.config import resolve_project_path
        project_path = resolve_project_path(manifest_path.parent.parent)
        if project_path is None:
            return

        from aah.core.standards.resolve import resolve_standards
        from aah.core.standards.embed import embed_standards
        from aah.core.common.io_utils import write_yaml

        # resolve_standards handles everything inline:
        # structured YAML + unstructured text extraction + domain brief + industry defaults
        resolved = resolve_standards(project_path)
        out_path = project_path / ".aah" / "plan" / "resolved-standards.yaml"
        write_yaml(resolved, out_path)
        print(f"Standards resolved: {resolved['summary']['total']} rules from {len(resolved['sources'])} source(s)", file=sys.stderr)

        result = embed_standards(project_path)
        if "error" not in result:
            print(f"Standards embedded into {result['features_updated']} feature(s)", file=sys.stderr)
    except Exception as e:
        # Non-fatal: standards are optional enrichment
        print(f"Warning: auto-resolve standards skipped: {e}", file=sys.stderr)


def transition_end(phase: str) -> None:
    """Handle phase END: advance current_phase and update progress next_steps."""
    manifest_path = find_manifest()
    if manifest_path is None:
        print("Error: manifest.yaml not found", file=sys.stderr)
        sys.exit(1)

    progress_path = find_progress()
    if progress_path is None:
        print("Error: claude-progress.json not found", file=sys.stderr)
        sys.exit(1)

    # At end of plan phase, auto-resolve and embed enterprise standards
    if phase == "plan":
        _auto_resolve_standards(manifest_path)

    # Advance current_phase to the next phase in sequence
    next_ph = _next_phase(phase)
    if next_ph:
        update_phase(manifest_path, next_ph)

    msg = PHASE_END_MESSAGES.get(phase, f"{phase.capitalize()} phase complete.")
    update_progress(progress_path, next_steps=msg)

    project_path = manifest_path.parent.parent
    push_to_remote(project_path, tags=True)

    print(f"Phase transition END → '{phase}' complete", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="AAH phase transition handler")
    sub = parser.add_subparsers(dest="command", required=True)

    start_p = sub.add_parser("start", help="Mark phase START")
    start_p.add_argument("phase", choices=PHASE_ORDER[1:])  # exclude "init"

    end_p = sub.add_parser("end", help="Mark phase END")
    end_p.add_argument("phase", choices=PHASE_ORDER[1:])

    args = parser.parse_args()

    if args.command == "start":
        transition_start(args.phase)
    elif args.command == "end":
        transition_end(args.phase)

    sys.exit(0)


if __name__ == "__main__":
    main()
