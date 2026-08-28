"""Tests for the read-only report commands extracted from orchestrator.py.

``format_qa_summary`` had zero coverage while it lived in the orchestrator. It is
pure presentation — dict in, string out — so it tests directly with no fixture.

Also pinned here: the reports read the same attested evidence the real gates
read, which is what stops a report from telling an operator "ready to merge"
while the gate blocks (and vice versa).
"""

from __future__ import annotations

import inspect

from aah.core.build import orchestrator_reports
from aah.core.build.orchestrator_reports import format_qa_summary


def test_qa_summary_returns_the_error_string_unframed():
    assert format_qa_summary({"error": "No QA report found for F001."}) == (
        "No QA report found for F001."
    )


def test_qa_summary_splits_criteria_and_lists_issues():
    out = format_qa_summary(
        {
            "feature_id": "F002",
            "verdict": "fail",
            "summary": {
                "criteria_passed": 1,
                "criteria_total": 3,
                "tests_run": 9,
                "tests_passed": 7,
            },
            "test_command": "pytest -q",
            "criteria": [
                {"id": "AC1", "description": "works", "verdict": "pass"},
                {"id": "AC2", "description": "broken", "verdict": "fail", "evidence": "tb"},
                {"id": "AC3", "description": "unknown", "verdict": "partial"},
            ],
            "issues": [{"severity": "high", "issue": "leak", "fix": "close it"}],
        }
    )
    assert "QA REPORT: F002 — FAIL" in out
    assert "Criteria checked:  1/3 passed" in out
    assert "Tests executed:    7/9 passed" in out
    assert "Test command:      pytest -q" in out
    assert "PASSED (1):" in out and "[PASS] AC1: works" in out
    assert "FAILED (1):" in out and "[FAIL] AC2: broken" in out
    assert "Evidence: tb" in out
    # A verdict that is neither pass nor fail is counted in neither list.
    assert "AC3" not in out
    assert "ISSUES REQUIRING FIXES (1):" in out
    assert "1. [HIGH] leak" in out and "→ Fix: close it" in out


def test_qa_summary_omits_optional_sections():
    out = format_qa_summary({"feature_id": "F001", "verdict": "pass"})
    assert "QA REPORT: F001 — PASS" in out
    assert "Criteria checked:  0/0 passed" in out
    assert "Tests executed" not in out
    assert "Test command" not in out
    assert "PASSED" not in out and "FAILED" not in out
    assert "ISSUES" not in out


def test_qa_summary_frames_with_a_rule_top_and_bottom():
    lines = format_qa_summary({"feature_id": "F001", "verdict": "pass"}).split("\n")
    assert lines[0] == "=" * 60
    assert lines[-1] == "=" * 60


def test_missing_plan_artifacts_report_an_error_not_a_crash(tmp_path):
    """A report runs before /aah-plan as easily as after; it must say what is
    missing rather than raise."""
    assert "waves.json not found" in orchestrator_reports.check_wave_readiness(
        tmp_path, 0
    )["error"]
    # No test-results directory yet → an empty summary, not a KeyError.
    assert orchestrator_reports.get_test_summary(tmp_path) == {
        "features": {},
        "regression": None,
        "total_passing": 0,
        "total_failing": 0,
    }
    # No dag.json → everything is "available" and nothing is blocked.
    assert orchestrator_reports.get_frontier_with_reasons(tmp_path) == {
        "available": [],
        "blocked": {},
    }


def test_reports_read_the_same_attested_evidence_as_the_gates():
    """The reason this module imports from ``verify`` rather than re-deriving.

    ``check_wave_readiness`` once called a raw regression check and could report
    LOOSER than the gate decided. It now reads the attested evidence and maps
    pass/fail/no_signal, so a refusal is neither passed nor failed.
    """
    src = inspect.getsource(orchestrator_reports)
    assert "read_regression_evidence" in src
    assert "validate_wave_artifacts" in src
    # QA comes from the authoritative attempt, not the derived display summary.
    assert "latest_attempt" in src
    assert "-qa.json" not in src
    # no_signal is a distinct outcome from failed.
    assert '"no_signal"' in src
