#!/usr/bin/env python3
"""Assemble feature-list.json from the .md feature contracts (one per module)."""

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.io_utils import write_json
from aah.core.common.validators import (
    validate_dict_schema,
    validate_feature_contract,
    FEATURE_SCHEMA,
)


def _run_contract_structural_validation(features: list[dict]) -> None:
    """Run the light structural feature-contract check; exit(1) on any violation.

    v2 (lean/TDD): there is no AC↔test-case coverage premise. This only verifies
    that any legacy acceptance_criteria / test_cases still present are lists with
    unique ids (see validate_feature_contract). New 5-section features carry
    neither field and pass trivially.
    """
    all_errors: list[str] = []
    for f in features:
        all_errors.extend(
            validate_feature_contract(f, context=f.get("id", "unknown"))
        )
    if all_errors:
        print("Feature contract validation errors:", file=sys.stderr)
        for err in all_errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)


def build_feature_list(features_dir: Path, output_path: Path) -> dict:
    """
    Read all .md frontmatter feature contracts from the features directory and
    create feature-list.json from scratch.

    This is a CREATE-ONLY operation. If feature-list.json already exists,
    sync_feature_md hook owns ongoing maintenance (adds new features,
    refreshes drifted fields). This function only runs for initial creation.

    For brownfield multi-iteration: collects cumulatively from iteration
    archives + current plan folder so all prior features are included.

    Each feature gets a 'passes: false' field per the RAPIDS harness pattern.
    """
    from aah.core.common.feature_list import load_all_features_cumulative

    # aah_path = .aah/plan/features -> .aah (same derivation as cumulative load)
    aah_path = features_dir.parent.parent
    # Guard: if JSON already exists, sync hook maintains it — not this function
    if output_path.exists():
        # The sync path MUST also be gated — otherwise a sync silently bypasses
        # the contract structural check. Fail closed BEFORE sync delegation.
        existing_features = load_all_features_cumulative(aah_path)
        _run_contract_structural_validation(existing_features)
        # JSON exists — run sync instead to handle any new/drifted features
        from aah.core.common.feature_list import sync_features_from_yaml
        result = sync_features_from_yaml(features_dir, output_path, feature_ids=None)
        return result

    # Cumulative load: includes iteration archives for brownfield
    features = load_all_features_cumulative(aah_path)
    if not features:
        print("Error: no feature files (.md) found", file=sys.stderr)
        sys.exit(1)

    # Validate each feature against schema
    all_errors = []
    for f in features:
        errors = validate_dict_schema(f, FEATURE_SCHEMA, context=f.get("id", "unknown"))
        all_errors.extend(errors)

    if all_errors:
        print("Feature validation errors:", file=sys.stderr)
        for err in all_errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)

    # Structural feature-contract check always fails closed before building.
    _run_contract_structural_validation(features)

    # Check for duplicate IDs
    ids = [f["id"] for f in features]
    duplicates = [fid for fid in ids if ids.count(fid) > 1]
    if duplicates:
        print(f"Error: duplicate feature IDs: {set(duplicates)}", file=sys.stderr)
        sys.exit(1)

    # Build features — copy all fields from YAML, add runtime status
    new_features = []
    for f in features:
        entry = dict(f)  # Copy all fields from YAML (source of truth)
        entry["passes"] = False  # Add runtime status field
        new_features.append(entry)

    return {"features": new_features}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build feature-list.json from feature YAMLs")
    parser.add_argument("--features-dir", type=Path, required=True, help="Path to features/ directory")
    parser.add_argument("--output", type=Path, required=True, help="Output feature-list.json path")
    parser.add_argument("--merge-existing", type=Path, default=None,
                        help="Deprecated: sync hook handles merging. Kept for backward compat (ignored).")
    args = parser.parse_args()

    if not args.features_dir.is_dir():
        print(f"Error: features directory not found: {args.features_dir}", file=sys.stderr)
        sys.exit(1)

    # Determine if this is a create or sync operation
    already_exists = args.output.exists()

    feature_list = build_feature_list(args.features_dir, args.output)

    # build_feature_list calls sync internally when JSON exists (writes directly),
    # but for fresh creation it returns the data — we write it here
    if not already_exists:
        write_json(feature_list, args.output)

    total = len(feature_list["features"])
    status = "synced" if already_exists else "built"
    json.dump({"status": status, "total_features": total, "output": str(args.output)}, sys.stdout, indent=2)
    print()
    print(f"{'Synced' if status == 'synced' else 'Built'} feature-list.json with {total} features", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
