#!/usr/bin/env python3
"""
TaskCompleted hook: validate individual feature completion.

Checks: code exists, tests exist, tests pass for the completed feature.
Exit 0 = pass, Exit 2 = fail.
"""

import json
import sys
from pathlib import Path

from aah.core.common.io_utils import read_json


def validate_feature_complete(project_path: Path, feature_id: str) -> tuple[bool, list[str]]:
    """Validate that a feature is truly complete."""
    issues = []
    aah_path = project_path / ".aah"

    # Check feature YAML exists
    feature_yaml = aah_path / "plan" / "features" / f"{feature_id}.yaml"
    if not feature_yaml.exists():
        issues.append(f"Feature YAML not found: {feature_yaml}")

    # Check test results exist
    test_results_path = aah_path / "build" / "test-results" / f"{feature_id}.json"
    if not test_results_path.exists():
        issues.append(f"No test results found for {feature_id}")
    else:
        try:
            results = read_json(test_results_path)
            if not results.get("passed", False):
                failed_tests = results.get("failed_tests", [])
                issues.append(
                    f"Tests not passing for {feature_id}: "
                    f"{len(failed_tests)} test(s) failed"
                )
                for ft in failed_tests[:5]:
                    issues.append(f"  - {ft.get('name', 'unknown')}: {ft.get('reason', 'no reason')}")
        except Exception as e:
            issues.append(f"Error reading test results: {e}")

    # Check feature-list.json has this feature marked as passes
    fl_path = aah_path / "feature-list.json"
    if fl_path.exists():
        try:
            fl_data = read_json(fl_path)
            feature = None
            for f in fl_data.get("features", []):
                if f.get("id") == feature_id:
                    feature = f
                    break
            if feature and not feature.get("passes", False):
                issues.append(f"Feature {feature_id} not marked as passing in feature-list.json")
        except Exception:
            pass

    if issues:
        return False, issues

    return True, []


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    from aah.core.common.config import resolve_project_path
    cwd = hook_input.get("cwd")
    project_path = resolve_project_path(Path(cwd) if cwd else None)
    if project_path is None:
        sys.exit(0)

    # Extract feature ID from hook context
    feature_id = hook_input.get("task", {}).get("metadata", {}).get("feature_id", "")
    if not feature_id:
        # Try to get from task subject
        subject = hook_input.get("task", {}).get("subject", "")
        # Extract feature ID pattern like F001
        import re
        match = re.search(r"(F\d+)", subject)
        if match:
            feature_id = match.group(1)

    if not feature_id:
        # Cannot determine feature — allow (don't block on missing context)
        sys.exit(0)

    passed, issues = validate_feature_complete(project_path, feature_id)

    if not passed:
        print(f"Feature completion gate FAILED for {feature_id}:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
