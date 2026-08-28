#!/usr/bin/env python3
"""
Read-only reporting subcommands for the build orchestrator.

These four reports — wave readiness, test summary, execution frontier, QA report
— answer "what is the state?" and never change it. They are separated from
``orchestrator.py`` because that module answers a different question ("what
happens next?") and only its answer is allowed to drive the build. Nothing here
is imported by ``compute_next_action``; ``orchestrator.py``'s CLI imports these
names for its subcommands, which keeps the dependency one-way.

A report that disagrees with the gate it describes is worse than no report, so
these read the same attested evidence the gates read — ``read_regression_evidence``
for regression status and ``qa_evidence.latest_attempt`` for QA, not the derived
display summaries that can drift from them.
"""

from __future__ import annotations

from pathlib import Path

from aah.core.build.qa_evidence import latest_attempt
from aah.core.build.verify import read_regression_evidence
from aah.core.common.dag import dag_from_json, get_execution_frontier
from aah.core.common.feature_list import load_feature_list
from aah.core.common.feature_utils import flatten_wave_features
from aah.core.common.io_utils import read_json
from aah.core.plan.compute_waves import flatten_waves


# ─── wave-readiness ──────────────────────────────────────────────────

def check_wave_readiness(project_path: Path, wave_num: int) -> dict:
    """Is this wave ready to merge? What's blocking?"""
    aah_path = project_path / ".aah"

    waves_path = aah_path / "plan" / "waves.json"
    if not waves_path.exists():
        return {"error": "waves.json not found. Run /aah-plan first."}
    waves_data = read_json(waves_path)
    waves = flatten_waves(waves_data)
    if wave_num >= len(waves):
        return {"error": f"Wave {wave_num} not found"}

    wave_feature_ids = flatten_wave_features(waves[wave_num])
    fl_data = load_feature_list(aah_path / "feature-list.json" if (aah_path / "feature-list.json").exists() else None)
    completed = {f["id"] for f in fl_data.get("features", []) if f.get("passes")}

    feature_status = {}
    for fid in wave_feature_ids:
        if fid in completed:
            feature_status[fid] = "passed"
        else:
            # Check test results
            tr = aah_path / "build" / "test-results" / f"{fid}.json"
            if tr.exists():
                result = read_json(tr)
                feature_status[fid] = "implemented" if result.get("passed") else "tests_failing"
            else:
                feature_status[fid] = "not_started"

    all_passed = all(s == "passed" for s in feature_status.values())

    regression, _reg_reason = read_regression_evidence(
        aah_path, project_path, wave_num
    )
    regression_status = "not_run"
    if regression:
        # Status before passed — a refusal is neither passed nor failed.
        status = regression.get("status", "pass" if regression.get("passed") else "fail")
        regression_status = {
            "pass": "passed",
            "fail": "failed",
        }.get(status, "no_signal")

    # Runtime validation status
    runtime_path = aah_path / "build" / "runtime-results" / f"wave-{wave_num}-all.json"
    if runtime_path.exists():
        rt_data = read_json(runtime_path)
        runtime_status = "passed" if rt_data.get("overall_passed") else "failed"
    else:
        runtime_status = "not_run"

    ready = (
        all_passed
        and regression_status == "passed"
        and runtime_status == "passed"
    )

    blockers = []
    if not all_passed:
        not_passed = [f for f, s in feature_status.items() if s != "passed"]
        blockers.append(f"Features not passing: {not_passed}")
    if regression_status == "not_run" and all_passed:
        blockers.append("Regression suite not yet run")
    if regression_status == "failed":
        blockers.append("Regression suite failed")
    if runtime_status == "not_run" and all_passed:
        blockers.append("Runtime validation not yet run")
    if runtime_status == "failed":
        blockers.append("Runtime validation failed")
    return {
        "wave": wave_num,
        "ready_to_merge": ready,
        "feature_status": feature_status,
        "regression": regression_status,
        "runtime_validation": runtime_status,
        "blockers": blockers,
        "next_action": "merge" if ready else (blockers[0] if blockers else "unknown"),
    }


# ─── test-summary ────────────────────────────────────────────────────

def get_test_summary(project_path: Path) -> dict:
    """Consolidated test results across all features."""
    aah_path = project_path / ".aah"
    test_dir = aah_path / "build" / "test-results"

    summary = {"features": {}, "regression": None, "total_passing": 0, "total_failing": 0}

    if not test_dir.is_dir():
        return summary

    for result_file in sorted(test_dir.glob("*.json")):
        if result_file.name == "regression-latest.json":
            summary["regression"] = read_json(result_file)
            continue
        if result_file.name == "test-execution-log.jsonl":
            continue

        fid = result_file.stem
        result = read_json(result_file)
        passed = result.get("passed", False)
        summary["features"][fid] = {
            "passed": passed,
            "error": result.get("error"),
        }
        if passed:
            summary["total_passing"] += 1
        else:
            summary["total_failing"] += 1

    return summary


# ─── frontier ────────────────────────────────────────────────────────

def get_frontier_with_reasons(project_path: Path) -> dict:
    """What's available, blocked, and why?"""
    aah_path = project_path / ".aah"

    fl_data = load_feature_list(aah_path / "feature-list.json" if (aah_path / "feature-list.json").exists() else None)
    features = fl_data.get("features", [])
    completed = {f["id"] for f in features if f.get("passes")}
    all_ids = {f["id"] for f in features}

    dag_path = aah_path / "plan" / "dag.json"
    if not dag_path.exists():
        return {"available": sorted(all_ids - completed), "blocked": {}}

    dag_data = read_json(dag_path)
    G = dag_from_json(dag_data)

    available = get_execution_frontier(G, completed)
    blocked = {}

    for fid in all_ids - completed - set(available):
        predecessors = set(G.predecessors(fid))
        missing = sorted(predecessors - completed)
        blocked[fid] = {
            "waiting_on": missing,
            "reason": f"Blocked by: {', '.join(missing)}",
        }

    # What completing each available feature would unblock
    unblock_impact = {}
    for fid in available:
        would_unblock = []
        for blocked_fid, info in blocked.items():
            remaining_deps = set(info["waiting_on"]) - {fid}
            if not remaining_deps:
                would_unblock.append(blocked_fid)
        if would_unblock:
            unblock_impact[fid] = would_unblock

    return {
        "completed": sorted(completed),
        "available": available,
        "blocked": blocked,
        "unblock_impact": unblock_impact,
    }


# ─── qa-report ───────────────────────────────────────────────────────

def get_qa_report(project_path: Path, feature_id: str) -> dict:
    """Read the latest verified QA attempt for a feature."""
    report = latest_attempt(project_path, feature_id)
    if report is None:
        return {"error": f"No QA report found for {feature_id}. QA has not run yet.", "feature_id": feature_id}
    return report


def format_qa_summary(report: dict) -> str:
    """Format a QA report into a human-readable summary for display in the main conversation."""
    if "error" in report:
        return report["error"]

    fid = report.get("feature_id", "?")
    verdict = report.get("verdict", "unknown").upper()
    summary = report.get("summary", {})
    criteria = report.get("criteria", [])
    issues = report.get("issues", [])

    lines = []
    lines.append("=" * 60)
    lines.append(f"QA REPORT: {fid} — {verdict}")
    lines.append("=" * 60)
    lines.append(f"Criteria checked:  {summary.get('criteria_passed', 0)}/{summary.get('criteria_total', 0)} passed")
    if summary.get("tests_run", 0) > 0:
        lines.append(f"Tests executed:    {summary.get('tests_passed', 0)}/{summary.get('tests_run', 0)} passed")
    if report.get("test_command"):
        lines.append(f"Test command:      {report['test_command']}")

    passed = [c for c in criteria if c.get("verdict") == "pass"]
    failed = [c for c in criteria if c.get("verdict") == "fail"]

    if passed:
        lines.append(f"\nPASSED ({len(passed)}):")
        for c in passed:
            lines.append(f"  [PASS] {c.get('id','?')}: {c.get('description','')}")

    if failed:
        lines.append(f"\nFAILED ({len(failed)}):")
        for c in failed:
            lines.append(f"  [FAIL] {c.get('id','?')}: {c.get('description','')}")
            if c.get("evidence"):
                lines.append(f"         Evidence: {c.get('evidence')}")

    if issues:
        lines.append(f"\nISSUES REQUIRING FIXES ({len(issues)}):")
        for i, issue in enumerate(issues, 1):
            sev = issue.get("severity", "").upper()
            lines.append(f"  {i}. [{sev}] {issue.get('issue', '')}")
            if issue.get("fix"):
                lines.append(f"     → Fix: {issue.get('fix')}")

    lines.append("=" * 60)
    return "\n".join(lines)
