#!/usr/bin/env python3
"""Read/write feature-list.json — synced from feature YAMLs, passes status protected."""

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.io_utils import read_json, read_yaml, write_json


FEATURE_LIST_FILENAME = "feature-list.json"
ITERATIONS_DIR = "iterations"


def load_all_features_cumulative(aah_path: Path) -> list[dict]:
    """Load feature YAMLs cumulatively across all iteration archives + current plan folder.

    For multi-iteration projects, feature YAMLs from completed iterations are
    archived into .aah/iterations/iter-N/plan/features/. This function collects
    from all sources so feature-list.json stays cumulative.

    Merge rule: if the same feature ID exists in both an archive and the current
    plan folder, the current folder version wins (latest iteration takes precedence).

    Activates whenever iteration archives exist (regardless of project type).
    If no iteration archives are present, falls back to current folder only.

    Thin wrapper over the canonical loader (feature_utils.load_features_from_dir,
    reached here via load_features_from_yamls): the ONLY behavioral difference is
    the iteration-archive merge below — everything else is identical parsing.
    """
    from aah.core.common.dag import load_features_from_yamls

    current_features_dir = aah_path / "plan" / "features"

    # Check if iteration archives exist
    iterations_dir = aah_path / ITERATIONS_DIR
    has_archives = iterations_dir.is_dir() and any(
        d.is_dir() and d.name.startswith("iter-") for d in iterations_dir.iterdir()
    ) if iterations_dir.is_dir() else False

    if not has_archives:
        return load_features_from_yamls(current_features_dir)

    # Collect from iteration archives (in order)
    all_features_by_id: dict[str, dict] = {}

    for iter_dir in sorted(iterations_dir.iterdir()):
        if iter_dir.is_dir() and iter_dir.name.startswith("iter-"):
            # Check both paths: plan/features/ (current) and features/ (legacy)
            archived_features_dir = iter_dir / "plan" / "features"
            if not archived_features_dir.is_dir():
                archived_features_dir = iter_dir / "features"  # legacy fallback
            for feature in load_features_from_yamls(archived_features_dir):
                all_features_by_id[feature["id"]] = feature

    # Current plan folder — overwrites archived versions (current wins)
    for feature in load_features_from_yamls(current_features_dir):
        all_features_by_id[feature["id"]] = feature

    return list(all_features_by_id.values())


def find_feature_list(start_dir: Path | None = None) -> Path | None:
    """Find feature-list.json. If start_dir given, walk up. Otherwise use config."""
    if start_dir:
        for directory in [start_dir, *start_dir.parents]:
            candidate = directory / ".aah" / FEATURE_LIST_FILENAME
            if candidate.exists():
                return candidate
        return None
    from aah.core.common.config import get_active_project_aah_path
    aah_path = get_active_project_aah_path()
    if aah_path:
        candidate = aah_path / FEATURE_LIST_FILENAME
        if candidate.exists():
            return candidate
    return None


def load_feature_list(path: Path | None = None) -> dict:
    """Load feature-list.json."""
    if path is None:
        path = find_feature_list()
    if path is None or not path.exists():
        return {"features": []}
    return read_json(path)


def save_feature_list(data: dict, path: Path) -> None:
    """Save feature-list.json."""
    write_json(data, path)


def get_feature_by_id(data: dict, feature_id: str) -> dict | None:
    """Get a feature by its ID."""
    for feature in data.get("features", []):
        if feature.get("id") == feature_id:
            return feature
    return None


def _update_feature_yaml_status(aah_path: Path, feature_id: str, status: str) -> None:
    """Update the status field in the source feature .md file and sync to feature-list.json."""
    from aah.core.common.feature_utils import find_feature_file

    features_dir = aah_path / "plan" / "features"
    if not features_dir.is_dir():
        return

    feature_file = find_feature_file(features_dir, feature_id)
    if feature_file and feature_file.exists():
        content = feature_file.read_text(encoding="utf-8")
        if content.startswith("---"):
            # YAML frontmatter format — update the status key in-place
            import re
            content = re.sub(r"^status:.*$", f"status: {status}", content, count=1, flags=re.MULTILINE)
            feature_file.write_text(content, encoding="utf-8")
        else:
            # Markdown section format — update or append a ## Status section
            from aah.core.common.feature_utils import append_section
            append_section(feature_file, "Status", [status])

    # Sync this feature to feature-list.json immediately
    fl_path = aah_path / "feature-list.json"
    if fl_path.exists():
        try:
            sync_features_from_yaml(features_dir, fl_path, feature_ids=[feature_id])
        except Exception:
            pass


def update_feature_status(path: Path, feature_id: str, passes: bool, skip_test_check: bool = False) -> dict:
    """Update only the 'passes' field for a feature. No structural changes allowed.

    When setting passes=True, requires test results to confirm the feature
    actually passed testing. This prevents marking features as passing
    without test execution proof.
    """
    data = load_feature_list(path)
    feature = get_feature_by_id(data, feature_id)
    if feature is None:
        print(f"Error: feature '{feature_id}' not found", file=sys.stderr)
        sys.exit(1)

    # Guard: verify test results exist and show passing before marking as true
    if passes and not skip_test_check:
        aah_path = path.parent  # feature-list.json lives in .aah/
        test_result_path = aah_path / "build" / "test-results" / f"{feature_id}.json"
        if not test_result_path.exists():
            print(
                f"Error: Cannot mark {feature_id} as passing — no test results found at {test_result_path}",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            from aah.core.build.verification_evidence import (
                FEATURE_TEST_PREFIX,
                read_attested,
            )

            test_data = read_attested(
                test_result_path, aah_path.parent, FEATURE_TEST_PREFIX
            )
            if test_data is None:
                print(
                    f"Error: Cannot mark {feature_id} as passing — test results "
                    "are unattested or unverifiable",
                    file=sys.stderr,
                )
                sys.exit(2)
            if not test_data.get("passed", False):
                print(
                    f"Error: Cannot mark {feature_id} as passing — test results show failure: "
                    f"{test_data.get('error', 'tests did not pass')}",
                    file=sys.stderr,
                )
                sys.exit(2)
        except Exception as e:
            print(f"Error: Cannot read test results for {feature_id}: {e}", file=sys.stderr)
            sys.exit(2)

    feature["passes"] = passes
    save_feature_list(data, path)

    # Update status in the source feature YAML
    _update_feature_yaml_status(path.parent, feature_id, "done" if passes else "in_progress")

    if passes:
        progress_path = path.parent / "claude-progress.json"
        if progress_path.exists():
            from aah.core.common.progress import update_progress
            update_progress(progress_path, completed_feature=feature_id)

    return data


def get_features_by_status(data: dict, passes: bool | None = None) -> list[dict]:
    """Filter features by their pass/fail status."""
    features = data.get("features", [])
    if passes is None:
        return features
    return [f for f in features if f.get("passes") == passes]


def get_pending_features(data: dict) -> list[dict]:
    """Get features that haven't passed yet."""
    return [f for f in data.get("features", []) if not f.get("passes", False)]


def get_passing_features(data: dict) -> list[dict]:
    """Get features that have passed."""
    return [f for f in data.get("features", []) if f.get("passes", False)]


def sync_features_from_yaml(features_dir: Path, feature_list_path: Path, feature_ids: list[str] | None = None) -> dict:
    """
    Sync feature-list.json from feature YAMLs.

    Args:
        features_dir: Path to directory containing feature YAML files
        feature_list_path: Path to feature-list.json
        feature_ids: Optional list of feature IDs to sync.
                     None = sync all features.
                     ["F001"] = sync only F001.
                     ["F001", "F003"] = sync those two.

    Behavior:
    - Updates target features from YAMLs, preserves 'passes'
    - New features (in YAML but not in JSON) get passes=False
    - For brownfield: collects cumulatively from iteration archives + current folder
    - When feature_ids=None and a YAML is removed, its entry is removed from JSON
    """
    # 1. Read feature YAMLs — cumulative for brownfield multi-iteration
    aah_path = features_dir.parent.parent  # .aah/plan/features -> .aah
    all_yaml_features = load_all_features_cumulative(aah_path)
    yaml_by_id = {f["id"]: f for f in all_yaml_features}

    # Filter to target features if specified
    if feature_ids is not None:
        target_ids = set(feature_ids)
    else:
        target_ids = set(yaml_by_id.keys())

    # 1b. Fail-closed deep contract gate — shared chokepoint for all writers.
    # Validates only the features being synced (target set) so unchanged
    # pre-existing features are untouched.
    # Raises ValidationError (not sys.exit) since this is a library function;
    # the CLI/callers surface the non-zero exit.
    from aah.core.common.validators import (
        validate_feature_contract,
        ValidationError,
    )

    contract_errors: list[str] = []
    for fid in sorted(target_ids):
        f = yaml_by_id.get(fid)
        if f is not None:
            contract_errors.extend(validate_feature_contract(f, context=fid))
    if contract_errors:
        raise ValidationError(
            "feature contract validation failed",
            errors=contract_errors,
        )

    # 2. Load existing feature-list.json (if exists)
    if feature_list_path.exists():
        existing = read_json(feature_list_path)
    else:
        existing = {"features": []}

    # 3. Build passes lookup from existing data
    passes_lookup = {f["id"]: f.get("passes", False) for f in existing.get("features", [])}

    # 4. Build updated feature list
    updated_features = []

    if feature_ids is None:
        # Full sync: output = all YAMLs (preserving passes) — removed YAMLs are dropped
        for yaml_feature in all_yaml_features:
            fid = yaml_feature["id"]
            entry = dict(yaml_feature)
            entry["passes"] = passes_lookup.get(fid, False)
            updated_features.append(entry)
    else:
        # Targeted sync: update only specified features, keep others unchanged
        # Start with existing features
        for existing_feature in existing.get("features", []):
            fid = existing_feature["id"]
            if fid in target_ids and fid in yaml_by_id:
                # Update this feature from YAML, preserve passes
                entry = dict(yaml_by_id[fid])
                entry["passes"] = existing_feature.get("passes", False)
                updated_features.append(entry)
            else:
                # Keep unchanged
                updated_features.append(existing_feature)

        # Add any new features from target_ids that weren't in existing
        existing_ids = {f["id"] for f in existing.get("features", [])}
        for fid in target_ids:
            if fid not in existing_ids and fid in yaml_by_id:
                entry = dict(yaml_by_id[fid])
                entry["passes"] = False
                updated_features.append(entry)

    # 5. Normalize applicable_standards: ensure it's always [{"total_rules": N, ...}]
    # The parser may produce ["Total rules: 0"] (bare strings) for zero-standards features
    for entry in updated_features:
        applicable = entry.get("applicable_standards")
        if isinstance(applicable, list) and applicable and isinstance(applicable[0], str):
            total = 0
            for item in applicable:
                if item.lower().startswith("total rules:"):
                    try:
                        total = int(item.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                    break
            entry["applicable_standards"] = [{"total_rules": total}]

    # 6. Write feature-list.json
    result = {"features": updated_features}
    write_json(result, feature_list_path)
    return result


def validate_json_matches_yamls(features_dir: Path, feature_list_path: Path) -> list[str]:
    """
    Compare feature-list.json against feature YAMLs.
    Return errors if JSON is stale/out-of-sync.
    Does NOT modify files — validation only.
    """
    errors = []

    # 1. Load all YAMLs — cumulative for brownfield multi-iteration
    aah_path = features_dir.parent.parent  # .aah/plan/features -> .aah
    all_yaml_features = load_all_features_cumulative(aah_path)
    yaml_by_id = {f["id"]: f for f in all_yaml_features}

    # 2. Load feature-list.json
    if not feature_list_path.exists():
        if all_yaml_features:
            errors.append("feature-list.json does not exist but feature YAMLs are present")
        return errors

    json_data = read_json(feature_list_path)
    json_features = json_data.get("features", [])
    json_by_id = {f["id"]: f for f in json_features}

    # 3. Check each YAML feature has matching JSON data
    for fid, yaml_data in yaml_by_id.items():
        if fid not in json_by_id:
            errors.append(f"Feature {fid} exists in YAML but missing from feature-list.json")
            continue

        json_feature = json_by_id[fid]
        for key, yaml_value in yaml_data.items():
            json_value = json_feature.get(key)
            if json_value != yaml_value:
                errors.append(
                    f"Feature {fid}: field '{key}' is stale in JSON "
                    f"(YAML={yaml_value!r}, JSON={json_value!r})"
                )

    # 4. Check for features in JSON without corresponding YAML
    for fid in json_by_id:
        if fid not in yaml_by_id:
            errors.append(f"Feature {fid} exists in JSON but has no YAML source file")

    return errors


def validate_structural_integrity(original: dict, proposed: dict) -> list[str]:
    """
    Compare original and proposed feature-list.json.
    Blocks feature removal and ID changes. Allows field additions/updates
    (legitimate YAML sync) and passes changes (status updates).

    For full source-of-truth validation against YAMLs, use
    validate_sync_integrity() in the gate instead.
    """
    errors = []

    orig_features = original.get("features", [])
    prop_features = proposed.get("features", [])

    # Build lookup by ID
    orig_by_id = {f["id"]: f for f in orig_features}
    prop_by_id = {f["id"]: f for f in prop_features}

    orig_ids = set(orig_by_id.keys())
    prop_ids = set(prop_by_id.keys())

    # Block feature removal — YAMLs are source of truth, removal needs explicit action
    removed = orig_ids - prop_ids
    if removed:
        errors.append(f"Features removed: {removed}. Removing features is not allowed.")

    # Block ID changes (a feature ID present in original but with different content
    # is fine — that's a YAML sync. But IDs must not disappear.)

    return errors


def validate_sync_integrity(original: dict, proposed: dict, features_dir: Path) -> list[str]:
    """
    Validate a proposed feature-list.json write against YAML source of truth.

    Same approach as passes validation — check against source of truth.

    Rules:
    1. passes changes → allowed (verified by test results elsewhere)
    2. Field changes that match current YAML state → allowed (legitimate sync)
    3. New feature added that has a YAML file → allowed
    4. Feature removal → blocked
    5. Field changes that DON'T match YAML state → blocked (unauthorized edit)
    """
    errors = []

    # Load all YAMLs as source of truth — cumulative for brownfield
    aah_path = features_dir.parent.parent  # .aah/plan/features -> .aah
    all_yaml_features = load_all_features_cumulative(aah_path)
    yaml_lookup = {f["id"]: f for f in all_yaml_features}

    orig_features = original.get("features", [])
    prop_features = proposed.get("features", [])

    orig_by_id = {f["id"]: f for f in orig_features}
    prop_by_id = {f["id"]: f for f in prop_features}

    orig_ids = set(orig_by_id.keys())
    prop_ids = set(prop_by_id.keys())

    # Check for removals
    removed = orig_ids - prop_ids
    if removed:
        errors.append(f"Features removed: {removed}. Removal not allowed.")

    # Validate each proposed feature against YAML source of truth
    for feature in prop_features:
        fid = feature.get("id")
        if not fid:
            errors.append("Feature entry missing 'id' field")
            continue

        yaml_data = yaml_lookup.get(fid)

        if yaml_data is None:
            # Feature in JSON but no YAML — only allowed if it was already there
            if fid not in orig_by_id:
                errors.append(f"Feature {fid} added without YAML source")
            continue

        # Compare each field (except passes) against YAML
        for key, value in feature.items():
            if key == "passes":
                continue  # passes handled by update_feature_status logic
            yaml_value = yaml_data.get(key)
            if yaml_value is not None and value != yaml_value:
                errors.append(
                    f"Feature {fid} field '{key}' doesn't match YAML "
                    f"(proposed={value!r}, yaml={yaml_value!r})"
                )

    return errors


def get_progress_summary(data: dict) -> dict:
    """Get a summary of feature completion progress."""
    features = data.get("features", [])
    total = len(features)
    passing = len([f for f in features if f.get("passes", False)])
    return {
        "total": total,
        "passing": passing,
        "failing": total - passing,
        "completion_pct": round(passing / total * 100, 1) if total > 0 else 0.0,
    }


def main() -> None:
    path_parent = argparse.ArgumentParser(add_help=False)
    path_parent.add_argument("--path", type=Path, default=None, help="Path to feature-list.json")

    parser = argparse.ArgumentParser(description="AAH feature list manager")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("read", help="Read feature list", parents=[path_parent])
    sub.add_parser("summary", help="Get progress summary", parents=[path_parent])
    sub.add_parser("pending", help="List pending features", parents=[path_parent])
    sub.add_parser("passing", help="List passing features", parents=[path_parent])

    update_p = sub.add_parser("update-status", help="Update a feature's pass/fail status", parents=[path_parent])
    update_p.add_argument("feature_id", type=str)
    update_p.add_argument("--passes", type=str, choices=["true", "false"], required=True)

    lifecycle_p = sub.add_parser("update-lifecycle", help="Update a feature's lifecycle status in its YAML (for GitHub label sync)", parents=[path_parent])
    lifecycle_p.add_argument("feature_id", type=str)
    lifecycle_p.add_argument("status", choices=["planned", "queued", "implementing", "in_qa", "done", "blocked", "rework", "cancelled"])

    validate_p = sub.add_parser("validate", help="Validate structural integrity between two files", parents=[path_parent])
    validate_p.add_argument("original", type=Path)
    validate_p.add_argument("proposed", type=Path)

    sync_p = sub.add_parser("sync", help="Sync feature-list.json from feature YAMLs", parents=[path_parent])
    sync_p.add_argument("--features-dir", type=Path, required=True, help="Path to features/ directory")
    sync_p.add_argument("--feature-ids", nargs="*", default=None, help="Specific feature IDs to sync (default: all)")

    args = parser.parse_args()
    fl_path = args.path

    if fl_path is None:
        fl_path = find_feature_list()
        if fl_path is None and args.command not in ("validate", "sync"):
            # For summary/read, return empty result instead of error
            if args.command in ("summary",):
                json.dump({"total": 0, "passing": 0, "failing": 0, "completion_pct": 0.0, "note": "No feature-list.json yet (created during planning phase)"}, sys.stdout, indent=2)
                print()
                sys.exit(0)
            if args.command in ("read", "pending", "passing"):
                json.dump({"features": []}, sys.stdout, indent=2)
                print()
                sys.exit(0)
            print("Error: feature-list.json not found", file=sys.stderr)
            sys.exit(1)

    if args.command == "read":
        data = load_feature_list(fl_path)
        json.dump(data, sys.stdout, indent=2)
        print()

    elif args.command == "summary":
        data = load_feature_list(fl_path)
        summary = get_progress_summary(data)
        json.dump(summary, sys.stdout, indent=2)
        print()

    elif args.command == "pending":
        data = load_feature_list(fl_path)
        pending = get_pending_features(data)
        json.dump(pending, sys.stdout, indent=2)
        print()

    elif args.command == "passing":
        data = load_feature_list(fl_path)
        passing = get_passing_features(data)
        json.dump(passing, sys.stdout, indent=2)
        print()

    elif args.command == "update-status":
        passes = args.passes == "true"
        update_feature_status(fl_path, args.feature_id, passes)
        print(f"Feature '{args.feature_id}' status -> passes={passes}", file=sys.stderr)

    elif args.command == "update-lifecycle":
        aah_path = fl_path.parent if fl_path else None
        if aah_path is None:
            print("Error: could not resolve .aah path", file=sys.stderr)
            sys.exit(1)
        _update_feature_yaml_status(aah_path, args.feature_id, args.status)
        print(f"Feature '{args.feature_id}' lifecycle -> {args.status}", file=sys.stderr)

        # Deterministic GitHub label push, scoped to this one feature.
        # Non-blocking — a failure here must never change the exit code of
        # update-lifecycle, since the local status write already succeeded.
        try:
            from aah.core.version_control.cli import sync_one_feature
            sync_one_feature(aah_path.parent, args.feature_id, label="lifecycle-update")
        except Exception:
            pass

    elif args.command == "validate":
        original = read_json(args.original)
        proposed = read_json(args.proposed)
        errors = validate_structural_integrity(original, proposed)
        if errors:
            json.dump({"valid": False, "errors": errors}, sys.stdout, indent=2)
            print()
            sys.exit(2)
        json.dump({"valid": True, "errors": []}, sys.stdout, indent=2)
        print()

    elif args.command == "sync":
        if not args.features_dir.is_dir():
            print(f"Error: features directory not found: {args.features_dir}", file=sys.stderr)
            sys.exit(1)
        if fl_path is None:
            print("Error: --path is required for sync (no feature-list.json found)", file=sys.stderr)
            sys.exit(1)
        # Fail closed on feature-contract violations. sys.exit(1)
        # escapes cli.py's blanket `except Exception -> exit(0)`; a bare raise
        # would be swallowed to exit 0 and silently bypass the gate.
        from aah.core.common.validators import ValidationError
        try:
            result = sync_features_from_yaml(args.features_dir, fl_path, args.feature_ids)
        except ValidationError as e:
            print("Feature contract validation errors:", file=sys.stderr)
            for err in e.errors:
                print(f"  - {err}", file=sys.stderr)
            sys.exit(1)
        total = len(result["features"])
        synced = args.feature_ids if args.feature_ids else "all"
        json.dump({"status": "synced", "total_features": total, "synced": synced}, sys.stdout, indent=2)
        print()
        print(f"Synced feature-list.json ({total} features)", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
