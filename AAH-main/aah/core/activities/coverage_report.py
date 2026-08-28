#!/usr/bin/env python3
"""
Activity coverage gap analysis per archetype/project type.

Reports which phases have activities for each project type and identifies gaps.

Usage:
  aah run core.activities.coverage_report
  aah run core.activities.coverage_report --project-type ai-infra-platforms
"""

import argparse
import json
import sys
from pathlib import Path

from aah.core.activities.loader import (
    filter_by_phase,
    load_activities_for_project_type,
    load_registry,
    resolve_activity_library_path,
)


PHASES = ["research", "analysis", "plan", "implement", "deploy", "sustain"]
MIN_ACTIVITIES_PER_PHASE = 2


def coverage_for_type(project_type: str, lib_path: Path | None = None) -> dict:
    """
    Compute activity coverage for a single project type.

    Returns dict with per-phase counts and gap indicators.
    """
    activities = load_activities_for_project_type([project_type], lib_path)

    phase_coverage = {}
    for phase in PHASES:
        phase_acts = filter_by_phase(activities, phase)
        count = len(phase_acts)
        phase_coverage[phase] = {
            "count": count,
            "activities": [a.get("id", "?") for a in phase_acts],
            "gap": count < MIN_ACTIVITIES_PER_PHASE,
        }

    total = sum(pc["count"] for pc in phase_coverage.values())
    gaps = [phase for phase, pc in phase_coverage.items() if pc["gap"]]

    return {
        "project_type": project_type,
        "total_activities": total,
        "phases": phase_coverage,
        "gap_phases": gaps,
        "has_gaps": len(gaps) > 0,
    }


def full_report(lib_path: Path | None = None) -> dict:
    """Generate coverage report for all project types in the registry."""
    if lib_path is None:
        lib_path = resolve_activity_library_path()
    registry = load_registry(lib_path)

    project_types = list(registry.get("project_types", {}).keys())
    reports = {}
    for pt in project_types:
        reports[pt] = coverage_for_type(pt, lib_path)

    return {
        "project_types": reports,
        "types_with_gaps": [pt for pt, r in reports.items() if r["has_gaps"]],
    }


def render_markdown(report: dict) -> str:
    """Render coverage report as markdown."""
    lines = ["# Activity Coverage Report", ""]

    for pt, data in report.get("project_types", {}).items():
        lines.append(f"## {pt}")
        lines.append(f"Total activities: {data['total_activities']}")
        if data["has_gaps"]:
            lines.append(f"**Gaps in phases: {', '.join(data['gap_phases'])}**")
        lines.append("")
        lines.append("| Phase | Count | Activities | Gap? |")
        lines.append("|-------|-------|-----------|------|")
        for phase in PHASES:
            pc = data["phases"].get(phase, {"count": 0, "activities": [], "gap": True})
            acts = ", ".join(pc["activities"][:3])
            if len(pc["activities"]) > 3:
                acts += f" +{len(pc['activities']) - 3}"
            gap = "YES" if pc["gap"] else ""
            lines.append(f"| {phase} | {pc['count']} | {acts} | {gap} |")
        lines.append("")

    types_with_gaps = report.get("types_with_gaps", [])
    if types_with_gaps:
        lines.append(f"## Summary: {len(types_with_gaps)} type(s) with gaps")
        for pt in types_with_gaps:
            lines.append(f"- **{pt}**: missing coverage in {', '.join(report['project_types'][pt]['gap_phases'])}")
    else:
        lines.append("## All project types have adequate coverage.")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Activity coverage report")
    parser.add_argument("--project-type", type=str, default=None)
    parser.add_argument("--format", choices=["json", "markdown"], default="markdown")
    args = parser.parse_args()

    if args.project_type:
        report = {"project_types": {args.project_type: coverage_for_type(args.project_type)}}
        report["types_with_gaps"] = [
            pt for pt, r in report["project_types"].items() if r["has_gaps"]
        ]
    else:
        report = full_report()

    if args.format == "json":
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print(render_markdown(report))

    sys.exit(0)


if __name__ == "__main__":
    main()
