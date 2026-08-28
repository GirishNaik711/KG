#!/usr/bin/env python3
"""Activate/deactivate DDRs by linking/unlinking them to activity YAML files."""

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.config import resolve_framework_root
from aah.core.common.io_utils import read_yaml, write_yaml
from aah.core.ddr_ops.loader import LAYERS, resolve_resources_path, load_ddr_by_id


def find_activity_for_layer(activity_lib: Path, archetype: str, layer_number: int) -> Path | None:
    """Find the activity YAML file that corresponds to a given archetype and layer.

    Searches analysis/ and research/ directories for activities that address
    the given layer (by convention, layer-specific activities include the layer
    number in their filename or decisions_addressed field).
    """
    # Search in analysis/ first (DDRs are typically closed during analysis)
    for phase in ["analysis", "research"]:
        phase_dir = activity_lib / phase / archetype
        if not phase_dir.exists():
            # Try with mapped directory names
            for subdir in (activity_lib / phase).iterdir() if (activity_lib / phase).exists() else []:
                if subdir.is_dir() and archetype in subdir.name:
                    phase_dir = subdir
                    break
            else:
                continue

        if not phase_dir.exists():
            continue

        for yaml_file in phase_dir.glob("*.yaml"):
            try:
                data = read_yaml(yaml_file)
                # Check if this activity addresses DDRs in the target layer
                decisions = data.get("decisions_addressed", [])
                for dec_id in decisions:
                    if f"L{layer_number}" in dec_id:
                        return yaml_file

                # Also check by filename pattern (e.g., L3-llm-selection.yaml)
                if f"L{layer_number}" in yaml_file.stem:
                    return yaml_file
            except Exception:
                continue

    return None


def find_activities_referencing_ddr(activity_lib: Path, ddr_id: str) -> list[dict]:
    """Find all activity YAML files that reference a specific DDR ID.

    Returns list of dicts with: activity_id, activity_name, file_path, phase
    """
    references = []

    for phase in ["research", "analysis"]:
        phase_dir = activity_lib / phase
        if not phase_dir.exists():
            continue
        for yaml_file in phase_dir.rglob("*.yaml"):
            try:
                data = read_yaml(yaml_file)
                decisions = data.get("decisions_addressed", [])
                if ddr_id in decisions:
                    references.append({
                        "activity_id": data.get("id", yaml_file.stem),
                        "activity_name": data.get("name", "Unknown"),
                        "file_path": str(yaml_file),
                        "phase": phase,
                    })
            except Exception:
                continue

    return references


def link_ddr_to_activity(activity_path: Path, ddr_id: str) -> dict:
    """Add a DDR ID to an activity's decisions_addressed list.

    Returns the updated activity data.
    """
    data = read_yaml(activity_path)

    if "decisions_addressed" not in data:
        data["decisions_addressed"] = []

    if ddr_id not in data["decisions_addressed"]:
        data["decisions_addressed"].append(ddr_id)
        # Sort DDR IDs for consistency
        data["decisions_addressed"].sort()
        write_yaml(data, activity_path)

    return data


def unlink_ddr_from_activity(activity_path: Path, ddr_id: str) -> dict:
    """Remove a DDR ID from an activity's decisions_addressed list.

    Returns the updated activity data.
    """
    data = read_yaml(activity_path)
    decisions = data.get("decisions_addressed", [])

    if ddr_id in decisions:
        decisions.remove(ddr_id)
        data["decisions_addressed"] = decisions
        write_yaml(data, activity_path)

    return data


def unlink_ddr_from_all(activity_lib: Path, ddr_id: str) -> list[str]:
    """Remove a DDR ID from all activities that reference it.

    Returns list of activity file paths that were modified.
    """
    references = find_activities_referencing_ddr(activity_lib, ddr_id)
    modified = []

    for ref in references:
        activity_path = Path(ref["file_path"])
        unlink_ddr_from_activity(activity_path, ddr_id)
        modified.append(ref["file_path"])

    return modified


def get_impact_analysis(activity_lib: Path, resources_path: Path, ddr_id: str) -> dict:
    """Analyze the impact of deleting a DDR.

    Returns: DDR details, referencing activities, and warnings.
    """
    # Load the DDR
    ddr = load_ddr_by_id(resources_path, ddr_id)

    # Find references
    references = find_activities_referencing_ddr(activity_lib, ddr_id)

    result = {
        "ddr_id": ddr_id,
        "ddr_found": ddr is not None,
        "decision_question": ddr.get("decision_question", "Unknown") if ddr else "DDR not found",
        "layer": ddr.get("layer", "Unknown") if ddr else "Unknown",
        "archetype": ddr.get("_archetype", "Unknown") if ddr else "Unknown",
        "referencing_activities": references,
        "reference_count": len(references),
        "warnings": [],
    }

    if references:
        result["warnings"].append(
            f"This DDR is referenced by {len(references)} activity(ies). "
            "Deleting it will leave broken references unless they are also updated."
        )

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="DDR activator — link/unlink DDRs to activities")
    sub = parser.add_subparsers(dest="command", required=True)

    # link
    link_p = sub.add_parser("link", help="Link a DDR to its matching activity")
    link_p.add_argument("--ddr-id", type=str, required=True, help="DDR ID to link")
    link_p.add_argument("--archetype", type=str, required=True)
    link_p.add_argument("--layer", type=int, required=True)
    link_p.add_argument("--activity-path", type=Path, default=None,
                        help="Explicit activity YAML path (auto-detected if omitted)")

    # unlink
    unlink_p = sub.add_parser("unlink", help="Remove a DDR from all activity references")
    unlink_p.add_argument("--ddr-id", type=str, required=True, help="DDR ID to unlink")

    # impact
    impact_p = sub.add_parser("impact", help="Analyze impact of removing a DDR")
    impact_p.add_argument("--ddr-id", type=str, required=True, help="DDR ID to analyze")

    # list-refs
    refs_p = sub.add_parser("list-refs", help="List all activities referencing a DDR")
    refs_p.add_argument("--ddr-id", type=str, required=True, help="DDR ID")

    args = parser.parse_args()

    framework_root = resolve_framework_root()
    if framework_root is None:
        print("Error: cannot determine framework root", file=sys.stderr)
        sys.exit(1)
    framework_root = Path(framework_root)
    activity_lib = framework_root / "activity-library"
    resources_path = resolve_resources_path(framework_root)

    if args.command == "link":
        if args.activity_path:
            activity_path = args.activity_path
        else:
            activity_path = find_activity_for_layer(activity_lib, args.archetype, args.layer)

        if activity_path is None:
            result = {
                "linked": False,
                "ddr_id": args.ddr_id,
                "error": f"No activity found for archetype '{args.archetype}' layer L{args.layer}. "
                         "Create an activity first with /rapids-activities add.",
            }
            json.dump(result, sys.stdout, indent=2)
            print()
            sys.exit(1)

        updated = link_ddr_to_activity(activity_path, args.ddr_id)
        result = {
            "linked": True,
            "ddr_id": args.ddr_id,
            "activity_id": updated.get("id", activity_path.stem),
            "activity_path": str(activity_path),
            "decisions_addressed": updated.get("decisions_addressed", []),
        }
        json.dump(result, sys.stdout, indent=2)
        print()

    elif args.command == "unlink":
        modified = unlink_ddr_from_all(activity_lib, args.ddr_id)
        result = {
            "unlinked": True,
            "ddr_id": args.ddr_id,
            "modified_activities": modified,
            "count": len(modified),
        }
        json.dump(result, sys.stdout, indent=2)
        print()

    elif args.command == "impact":
        result = get_impact_analysis(activity_lib, resources_path, args.ddr_id)
        json.dump(result, sys.stdout, indent=2)
        print()

    elif args.command == "list-refs":
        references = find_activities_referencing_ddr(activity_lib, args.ddr_id)
        json.dump({"ddr_id": args.ddr_id, "references": references}, sys.stdout, indent=2)
        print()


if __name__ == "__main__":
    main()
