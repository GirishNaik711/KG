#!/usr/bin/env python3
"""
Generate a phase-level summary for AAH audit trail.

Collects per-phase: artifacts produced, Q&A rounds, activity completion,
duration from git log timestamps. Saves to .aah/audit/phase-{phase}-summary.md.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.io_utils import read_json, read_yaml


def _git(args: list, cwd: Path) -> str:
    result = subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


def generate(project_path: Path, phase: str) -> str:
    """Generate a phase-level summary."""
    aah_root = project_path / ".aah"
    lines = []

    def a(line: str = "") -> None:
        lines.append(line)

    a(f"# Phase Summary: {phase}")
    a(f"_Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_")
    a("")

    # Artifacts produced
    phase_dir = aah_root / phase
    artifact_count = 0
    if phase_dir.is_dir():
        a("## Artifacts Produced")
        a("")
        for f in sorted(phase_dir.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                rel = f.relative_to(aah_root)
                a(f"- `.aah/{rel}`")
                artifact_count += 1
        a("")
    a(f"_Total: {artifact_count} artifact(s)_")
    a("")

    # Q&A rounds for this phase
    intake_path = aah_root / "intake.json"
    if intake_path.exists():
        try:
            intake = read_json(intake_path)
            phase_rounds = [r for r in intake.get("rounds", []) if r.get("phase") == phase]
            if phase_rounds:
                a("## Questions & Answers")
                a("")
                for round_entry in phase_rounds:
                    for qa in round_entry.get("questions", []):
                        q = qa.get("question")
                        answer = qa.get("answer")
                        if q and answer:
                            a(f"- **Q:** {q}")
                            a(f"  **A:** {answer}")
                a("")
        except Exception:
            pass

    # Activity completion status
    activity_plan_path = aah_root / "activity-plan.yaml"
    if activity_plan_path.exists():
        try:
            plan = read_yaml(activity_plan_path)
            phase_activities = [
                act for act in plan.get("activities", [])
                if act.get("phase") == phase
            ]
            if phase_activities:
                a("## Activity Completion")
                a("")
                a("| Activity | Status |")
                a("|----------|--------|")
                for act in phase_activities:
                    a(f"| {act.get('id', '?')} — {act.get('name', '?')} | {act.get('status', 'pending')} |")
                a("")
        except Exception:
            pass

    # Duration from git log
    tag_name = None
    project_name = ""
    manifest_path = aah_root / "manifest.yaml"
    if manifest_path.exists():
        try:
            manifest = read_yaml(manifest_path)
            project_name = manifest.get("project_name", "")
            tag_name = f"{project_name}/aah-{phase}"
        except Exception:
            pass

    if tag_name:
        # Check if tag exists
        tag_check = _git(["tag", "-l", tag_name], cwd=project_path)
        if tag_check:
            tag_date = _git(["log", "-1", "--format=%aI", tag_name], cwd=project_path)
            if tag_date:
                a(f"## Timeline")
                a(f"- Phase tag: `{tag_name}`")
                a(f"- Completed: {tag_date[:10]}")
                a("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate phase summary")
    parser.add_argument("--phase", type=str, required=True)
    parser.add_argument("--project-path", type=Path, default=None)
    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(args.project_path)
    aah_root = project_path / ".aah"

    summary = generate(project_path, args.phase)

    # Save to audit trail
    audit_dir = aah_root / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    out_file = audit_dir / f"phase-{args.phase}-summary.md"
    out_file.write_text(summary, encoding='utf-8')
    print(f"Phase summary saved to {out_file}", file=sys.stderr)
    print(summary)
    sys.exit(0)


if __name__ == "__main__":
    main()
