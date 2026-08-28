#!/usr/bin/env python3
"""
PostToolUse hook: sync feature-list.json when a feature .md file is written/edited.

Smart sync logic:
1. If feature-list.json doesn't exist → skip (build_feature_list owns creation)
2. If feature-list.json exists → compare state:
   a. If prior features have drifted fields → full sync (rebuild all, preserve passes)
   b. If only new features missing → targeted sync (add only new ones, passes=False)
   c. If everything in sync → skip (no work needed)

Exit 0 = success (or not applicable), Exit 1 = error.
"""

import json
import sys
from pathlib import Path


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    tool_input = hook_input.get("tool_input", {})
    tool_name = hook_input.get("tool_name", "")

    # Determine the file path based on tool type
    if tool_name == "Write":
        file_path = tool_input.get("file_path", "")
    elif tool_name == "Edit":
        file_path = tool_input.get("file_path", "")
    else:
        sys.exit(0)

    if not file_path:
        sys.exit(0)

    # Only trigger for feature .md files in .aah/plan/features/
    path = Path(file_path)
    if not (path.suffix == ".md" and "plan/features" in str(path).replace("\\", "/")):
        sys.exit(0)

    # Resolve the features directory and feature-list.json path
    # The feature file is at: <project>/.aah/plan/features/<id>.md
    features_dir = path.parent
    aah_dir = features_dir.parent.parent  # .aah/plan/features -> .aah
    feature_list_path = aah_dir / "feature-list.json"

    if not features_dir.is_dir():
        sys.exit(0)

    # Gate: if feature-list.json doesn't exist, skip — build_feature_list owns creation
    if not feature_list_path.exists():
        sys.exit(0)

    # Smart sync: determine what kind of sync is needed
    from aah.core.common.feature_list import sync_features_from_yaml
    from aah.core.common.dag import load_features_from_yamls
    from aah.core.common.io_utils import read_json

    try:
        # Load current iteration features only for detection — archived iterations
        # are immutable and can't drift. The underlying sync_features_from_yaml
        # handles cumulativity internally when performing the actual sync.
        all_md_features = load_features_from_yamls(features_dir)
        md_by_id = {f["id"]: f for f in all_md_features}

        existing_json = read_json(feature_list_path)
        json_features = existing_json.get("features", [])
        json_by_id = {f["id"]: f for f in json_features}

        # Determine: new features to add + drifted features
        new_features_to_add = []
        drifted_features = []

        for fid, md_data in md_by_id.items():
            if fid not in json_by_id:
                new_features_to_add.append(fid)
            else:
                # Check if any field (except passes) has drifted
                json_entry = json_by_id[fid]
                for key, md_value in md_data.items():
                    if key == "passes":
                        continue
                    if json_entry.get(key) != md_value:
                        drifted_features.append(fid)
                        break

        # Decide action
        if drifted_features:
            # Full sync: prior features have stale fields
            result = sync_features_from_yaml(features_dir, feature_list_path, feature_ids=None)
            total = len(result["features"])
            print(
                f"Full sync: {len(drifted_features)} drifted, "
                f"{len(new_features_to_add)} new → {total} total features",
                file=sys.stderr,
            )
        elif new_features_to_add:
            # Targeted sync: only add new features
            result = sync_features_from_yaml(
                features_dir, feature_list_path, feature_ids=new_features_to_add
            )
            total = len(result["features"])
            print(
                f"Targeted sync: added {len(new_features_to_add)} new feature(s) → {total} total",
                file=sys.stderr,
            )
        else:
            # Already in sync — no work needed
            sys.exit(0)

    except Exception as e:
        print(f"Warning: feature sync failed: {e}", file=sys.stderr)
        # Don't block the tool — sync failure is non-fatal
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
