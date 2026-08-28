#!/usr/bin/env python3
"""
Validate all criteria for merging develop to main.

Checks: all features pass, regression suite passes, no in-progress features.
Produces a merge readiness report. Exit 0 = ready, Exit 2 = not ready.
"""

import argparse
import json
import sys
from pathlib import Path

from aah.core.build.verification_evidence import NO_VERDICT_STATUSES
from aah.core.common.feature_list import get_progress_summary, load_feature_list
from aah.core.common.io_utils import read_json
from aah.core.common.progress import load_progress


def validate_main_merge(project_path: Path) -> dict:
    """Validate all criteria for merging to main."""
    aah_path = project_path / ".aah"
    issues = []
    checks = {}

    # Check 1: All features have passes: true
    fl_path = aah_path / "feature-list.json"
    if fl_path.exists():
        fl_data = load_feature_list(fl_path)
        summary = get_progress_summary(fl_data)
        checks["feature_completion"] = summary

        if summary["failing"] > 0:
            failing_features = [f["id"] for f in fl_data["features"] if not f.get("passes")]
            issues.append(
                f"{summary['failing']} feature(s) not passing: {failing_features[:10]}"
            )
    else:
        issues.append("No feature-list.json found")

    # Check 2: Regression suite passes
    #
    # `status` before `passed`: a no-verdict run (no_signal, wrong branch) carries
    # `passed: false` while having measured nothing. That is not a failing suite —
    # it still blocks the merge, but reporting it as "not passing" sends whoever
    # reads this report off to repair a failure that does not exist.
    regression_path = aah_path / "build" / "test-results" / "regression-latest.json"
    if regression_path.exists():
        regression = read_json(regression_path)
        status = regression.get("status", "pass" if regression.get("passed") else "fail")
        checks["regression"] = {"passed": regression.get("passed", False), "status": status}
        if status in NO_VERDICT_STATUSES:
            reason = regression.get("signal_reason") or status
            issues.append(f"Regression rendered no verdict ({reason}) — re-run it")
        elif status != "pass":
            issues.append("Regression suite not passing")
    else:
        issues.append("No regression test results found")

    # Check 3: No in-progress features
    progress_path = aah_path / "claude-progress.json"
    if progress_path.exists():
        progress = load_progress(progress_path)
        in_progress = progress.get("in_progress_features", [])
        checks["in_progress"] = in_progress
        if in_progress:
            issues.append(f"Features still in progress: {in_progress}")

        # Check 4: No known issues
        known_issues = progress.get("known_issues", [])
        checks["known_issues"] = known_issues
        if known_issues:
            issues.append(f"{len(known_issues)} known issue(s) remain")

    # Check 5: Runtime validation passed on every CHECKPOINT wave
    #
    # Gated on is_checkpoint_wave, the helper the orchestrator, verify.py and the
    # wave merge all read. A non-checkpoint wave produces no runtime evidence by
    # design, so demanding it here blocks a correctly-built project at the final
    # gate — and "missing for wave N" reads as an instruction to run the very
    # validation the cadence forbids.
    waves_path = aah_path / "plan" / "waves.json"
    if waves_path.exists():
        waves_data = read_json(waves_path)
        from aah.core.build.orchestrator import is_checkpoint_wave
        from aah.core.plan.compute_waves import flatten_waves
        waves = flatten_waves(waves_data)
        runtime_checks = {}
        for wave_num in range(len(waves)):
            if not is_checkpoint_wave(aah_path, wave_num):
                continue
            runtime_path = aah_path / "build" / "runtime-results" / f"wave-{wave_num}-all.json"
            if not runtime_path.exists():
                runtime_checks[wave_num] = "missing"
                issues.append(f"Runtime validation missing for wave {wave_num}")
            else:
                rt_data = read_json(runtime_path)
                if rt_data.get("overall_passed"):
                    runtime_checks[wave_num] = "passed"
                else:
                    runtime_checks[wave_num] = "failed"
                    issues.append(f"Runtime validation failed for wave {wave_num}")
        checks["runtime_validation"] = runtime_checks

    # No artifact-completeness check. It reran a file-presence scan for every
    # historical wave, duplicating what verify.verify_wave_evidence already
    # established — with attestation and freshness — at each wave's promotion.
    # Checks 1-5 above are unchanged.

    ready = len(issues) == 0

    return {
        "ready": ready,
        "issues": issues,
        "checks": checks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate readiness for main merge")
    parser.add_argument("--project-path", type=Path, default=None)
    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(args.project_path)

    result = validate_main_merge(project_path)
    json.dump(result, sys.stdout, indent=2)
    print()

    if result["ready"]:
        print("Main merge: READY", file=sys.stderr)
        sys.exit(0)
    else:
        print("Main merge: NOT READY", file=sys.stderr)
        for issue in result["issues"]:
            print(f"  - {issue}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
