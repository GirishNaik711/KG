#!/usr/bin/env python3
"""
TeammateIdle hook: validate cumulative regression tests pass.

Before allowing a teammate to pick up new work, ensure all existing
tests still pass. Exit 0 = pass, Exit 2 = fail.

Read-only about cadence: this hook reports on an existing regression verdict and
never asks for a run. Whether regression runs at all is the orchestrator's call
(last wave only), so no wave awareness is needed here — on earlier waves there is
no artifact and this is a no-op.
"""

import json
import sys
from pathlib import Path

from aah.core.build.verification_evidence import NO_VERDICT_STATUSES
from aah.core.common.io_utils import read_json


def validate_regression_pass(project_path: Path) -> tuple[bool, list[str]]:
    """Block only on a real `fail` verdict from an existing regression run."""
    issues = []
    aah_path = project_path / ".aah"

    # Look for cumulative regression results
    test_results_dir = aah_path / "build" / "test-results"
    if not test_results_dir.is_dir():
        # No test results yet — allow (early in project)
        return True, []

    regression_file = test_results_dir / "regression-latest.json"
    if not regression_file.exists():
        # No artifact means regression has not been authorized yet, NOT a
        # skipped gate. Regression is last-wave-only (orchestrator.py gates the
        # `run_regression` action on is_last_wave), so on every earlier wave the
        # absence IS the cadence. This gate must never ask for it: an idle
        # teammate reading a failed gate that says "run the regression suite"
        # complies, and that produced unauthorised early-wave runs whose
        # artifacts then poisoned later consumers.
        return True, []

    try:
        results = read_json(regression_file)
    except Exception as e:
        issues.append(f"Error reading regression results: {e}")
        return False, issues

    # Gate on `status`, not `passed` alone (the contract stated in
    # run_regression_suite's module docstring, and what orchestrator/verify do).
    # A no-verdict run carries `passed: false` while having measured nothing, so
    # reading `passed` alone reports a suite failure that never happened — and,
    # because the artifact stays on disk, reports it on every idle forever.
    status = results.get("status", "pass" if results.get("passed") else "fail")
    if status in NO_VERDICT_STATUSES:
        return True, []

    if status == "fail":
        total = results.get("total_tests", 0)
        failed = results.get("failed_count", 0)
        issues.append(f"Regression suite failed: {failed}/{total} tests failed")

        for failure in results.get("failures", [])[:10]:
            issues.append(f"  - {failure.get('test', 'unknown')}: {failure.get('reason', '')}")

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

    passed, issues = validate_regression_pass(project_path)

    if not passed:
        print("Regression gate FAILED:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        print("\nFix regression failures before starting new work.", file=sys.stderr)
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
