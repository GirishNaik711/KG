#!/usr/bin/env python3
"""
Validates that research phase properly maps DDRs to EVD files.

RULE: Each DDR referenced in activity tasks must have exactly one EVD file.
ANTI-PATTERN: Consolidated EVD files covering multiple DDRs are not allowed.

Usage:
    aah run core.research.validate_evd_mapping \\
        --activity-plan /path/to/.aah/activity-plan.yaml \\
        --evidence-dir /path/to/.aah/research/evidence
"""

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Set

from aah.core.common.io_utils import read_yaml


def extract_ddr_refs_from_activity_plan(activity_plan_path: Path) -> Dict[str, List[str]]:
    """
    Extract all DDR references from the activity plan.

    Returns:
        Dict mapping activity_id -> list of ddr_refs
    """
    plan = read_yaml(activity_plan_path)

    ddr_map = {}
    research_activities = plan.get("research_activities", {})

    for activity_id, activity_data in research_activities.items():
        if activity_data.get("status") != "completed":
            continue

        # Get the depth level
        depth = activity_data.get("depth", "standard")

        # This would require loading the full activity definition to get tasks
        # For now, we'll scan the artifacts_produced field
        artifacts = activity_data.get("artifacts_produced", [])

        # Extract DDR IDs from artifact names
        ddrs = []
        for artifact in artifacts:
            # Look for patterns like EVD-L7-001 in the filename
            parts = Path(artifact).stem.split("-")
            if len(parts) >= 3 and parts[0] == "EVD" and parts[1].startswith("L"):
                ddr_id = f"DDR-{parts[1]}-{parts[2]}"
                ddrs.append(ddr_id)

        if ddrs:
            ddr_map[activity_id] = ddrs

    return ddr_map


def find_evd_files(evidence_dir: Path) -> Set[str]:
    """
    Find all EVD files in the evidence directory.

    Returns:
        Set of EVD file basenames (e.g., "EVD-L7-001.md")
    """
    if not evidence_dir.exists():
        return set()

    evd_files = set()
    for file_path in evidence_dir.glob("EVD-*.md"):
        evd_files.add(file_path.name)

    return evd_files


def validate_evd_mapping(activity_plan_path: Path, evidence_dir: Path) -> bool:
    """
    Validate that each DDR has exactly one corresponding EVD file.

    Returns:
        True if valid, False otherwise
    """
    print("Validating EVD file mapping...", file=sys.stderr)

    # Get expected DDRs from activity plan
    ddr_map = extract_ddr_refs_from_activity_plan(activity_plan_path)

    # Get actual EVD files
    evd_files = find_evd_files(evidence_dir)

    # Track violations
    violations = []
    all_valid = True

    # Check for consolidated files (anti-pattern)
    for evd_file in evd_files:
        if "consolidated" in evd_file.lower():
            violations.append({
                "type": "CONSOLIDATED_FILE",
                "file": evd_file,
                "message": f"Consolidated EVD file detected: {evd_file}. "
                          f"Each DDR must have its own separate EVD file."
            })
            all_valid = False

    # Check for missing EVD files
    for activity_id, ddr_list in ddr_map.items():
        for ddr_id in ddr_list:
            # Expected EVD filename: EVD-L7-001.md for DDR-L7-001
            expected_evd = f"EVD-{ddr_id.replace('DDR-', '')}.md"

            # Also check with descriptive suffix patterns
            matching_files = [
                f for f in evd_files
                if f.startswith(f"EVD-{ddr_id.replace('DDR-', '')}")
            ]

            if not matching_files:
                violations.append({
                    "type": "MISSING_EVD",
                    "ddr": ddr_id,
                    "activity": activity_id,
                    "expected": expected_evd,
                    "message": f"Missing EVD file for {ddr_id} (activity: {activity_id}). "
                              f"Expected: {expected_evd}"
                })
                all_valid = False
            elif len(matching_files) > 1:
                violations.append({
                    "type": "DUPLICATE_EVD",
                    "ddr": ddr_id,
                    "files": matching_files,
                    "message": f"Multiple EVD files found for {ddr_id}: {matching_files}. "
                              f"Each DDR must have exactly one EVD file."
                })
                all_valid = False

    # Report violations
    if violations:
        print(f"\n❌ Found {len(violations)} EVD mapping violation(s):\n", file=sys.stderr)
        for i, violation in enumerate(violations, 1):
            print(f"{i}. [{violation['type']}]", file=sys.stderr)
            print(f"   {violation['message']}\n", file=sys.stderr)

        return False
    else:
        print("✅ All DDRs properly mapped to EVD files.\n", file=sys.stderr)
        return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate DDR-to-EVD file mapping in research phase"
    )
    parser.add_argument(
        "--activity-plan",
        type=Path,
        required=True,
        help="Path to activity-plan.yaml"
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        required=True,
        help="Path to .aah/research/evidence/ directory"
    )

    args = parser.parse_args()

    if not args.activity_plan.exists():
        print(f"Error: Activity plan not found: {args.activity_plan}", file=sys.stderr)
        sys.exit(1)

    if not args.evidence_dir.exists():
        print(f"Error: Evidence directory not found: {args.evidence_dir}", file=sys.stderr)
        sys.exit(1)

    valid = validate_evd_mapping(args.activity_plan, args.evidence_dir)

    sys.exit(0 if valid else 1)


if __name__ == "__main__":
    main()
