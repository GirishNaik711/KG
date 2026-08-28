#!/usr/bin/env python3
"""
PreToolUse hook: protect feature-list.json structural integrity.

Reads proposed Write content from hook stdin, compares against current file.
Validates proposed changes against feature YAML source of truth.

Rules:
- passes changes → allowed (verified by test results elsewhere)
- Field changes matching YAML state → allowed (legitimate sync)
- New features with YAML source → allowed
- Feature removal → blocked
- Field changes not matching YAML → blocked (unauthorized edit)

Exit 0 = valid, Exit 2 = invalid.
"""

import json
import sys
from pathlib import Path

from aah.core.common.feature_list import validate_structural_integrity, validate_sync_integrity
from aah.core.common.io_utils import read_json


def _find_features_dir(feature_list_path: Path) -> Path | None:
    """Locate the features directory relative to feature-list.json.

    feature-list.json lives at .aah/feature-list.json
    features dir lives at .aah/plan/features/
    """
    aah_dir = feature_list_path.parent
    features_dir = aah_dir / "plan" / "features"
    if features_dir.is_dir():
        return features_dir
    return None


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    tool_input = hook_input.get("tool_input", {})
    file_path = tool_input.get("file_path", "")

    # Only guard feature-list.json
    if "feature-list.json" not in file_path:
        sys.exit(0)

    # Get the proposed content
    proposed_content = tool_input.get("content", "")
    if not proposed_content:
        sys.exit(0)

    # Parse proposed content as JSON
    try:
        proposed = json.loads(proposed_content)
    except json.JSONDecodeError:
        print("Error: proposed feature-list.json is not valid JSON", file=sys.stderr)
        sys.exit(2)

    # Read the current file
    current_path = Path(file_path)
    if not current_path.exists():
        # First write — allow
        sys.exit(0)

    try:
        current = read_json(current_path)
    except Exception:
        # Can't read current — allow (might be corrupted)
        sys.exit(0)

    # First: basic structural check (no feature removal)
    structural_errors = validate_structural_integrity(current, proposed)
    if structural_errors:
        print("feature-list.json structural integrity violation:", file=sys.stderr)
        for err in structural_errors:
            print(f"  - {err}", file=sys.stderr)
        print(
            "\nFeature removal is not allowed. Feature YAMLs are the source of truth.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Second: validate field changes against YAML source of truth
    features_dir = _find_features_dir(current_path)
    if features_dir is not None:
        sync_errors = validate_sync_integrity(current, proposed, features_dir)
        if sync_errors:
            print("feature-list.json sync integrity violation:", file=sys.stderr)
            for err in sync_errors:
                print(f"  - {err}", file=sys.stderr)
            print(
                "\nField values must match feature YAML source of truth. "
                "Use 'aah run core.common.feature_list sync' to sync from YAMLs.",
                file=sys.stderr,
            )
            sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
