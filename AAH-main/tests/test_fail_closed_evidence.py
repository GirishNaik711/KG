"""Fail-closed quality-check status vocabulary and checkpoint evidence.

NO MOCKS — real temp projects, real subprocess, real node/npm/pytest (all available).
Does NOT use the broken `rapids_project` conftest fixture (it imports
aah.core.scaffold.aah_dir which doesn't exist → baseline ModuleNotFoundError).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aah.core.build.quality_checks import (
    get_quality_summary,
    run_coverage_check,
    run_linting,
    run_static_analysis,
    run_type_check,
)
from aah.core.build.verify import subject_identity
from aah.core.common.attestation import write_attested
from aah.core.common.io_utils import read_json, write_json, write_yaml
from tests._qa_helpers import seed_qa_attempt
from tests.support.aah_project import AAHProjectBuilder


def _write_manifest(tmp_path: Path) -> None:
    AAHProjectBuilder(tmp_path).manifest(project_name="test")


def _node_project_no_coverage(tmp_path: Path) -> Path:
    """A Node project with only a lint script, no test:coverage → adapter returns None."""
    package_json = {
        "name": "test",
        "scripts": {
            "lint": "echo 'no linter'",
        },
    }
    AAHProjectBuilder(tmp_path).file("package.json", json.dumps(package_json))
    return tmp_path


def _real_cov_project(tmp_path: Path, pct: float) -> Path:
    """A Node project with test:coverage echoing istanbul line."""
    package_json = {
        "name": "test",
        "scripts": {
            "test:coverage": f"echo 'All files | {pct} | 100 | 100 | 100 |'",
        },
    }
    AAHProjectBuilder(tmp_path).file("package.json", json.dumps(package_json))
    return tmp_path


def _bare_python_project(tmp_path: Path) -> Path:
    """A bare Python project (no mypy, no coverage) → adapter returns None."""
    AAHProjectBuilder(tmp_path).file("pyproject.toml", "[project]\nname = 'test'\n")
    return tmp_path


def test_missing_coverage_tool_reports_no_signal(tmp_path):
    """AC1: no-coverage project, no flag → run_coverage_check status=='no_signal' and != 'pass'."""
    _node_project_no_coverage(tmp_path)
    _write_manifest(tmp_path)

    result = run_coverage_check(tmp_path, "F001")

    assert result["status"] == "no_signal"
    assert result["status"] != "pass"
    assert result["details"]["skip_reason"] == "no_coverage_tool"


def test_every_check_reports_status_from_vocabulary(tmp_path):
    """AC2: run all 4 checks on a real bare project → each status in {pass,fail,no_signal,not_applicable}."""
    _bare_python_project(tmp_path)
    _write_manifest(tmp_path)

    linting = run_linting(tmp_path, "F001")
    sa = run_static_analysis(tmp_path, "F001")
    cov = run_coverage_check(tmp_path, "F001")
    tc = run_type_check(tmp_path, "F001")

    from aah.core.build.quality_checks import QUALITY_STATUSES
    assert linting["status"] in QUALITY_STATUSES
    assert sa["status"] in QUALITY_STATUSES
    assert cov["status"] in QUALITY_STATUSES
    assert tc["status"] in QUALITY_STATUSES


def test_no_signal_never_satisfies_gate_and_not_applicable_is_deterministic(tmp_path):
    """AC3: (a) enforced + no-coverage → no_signal, passed=False, overall_passed=False;
    (b) adapter-None skip (bare python no mypy) → not_applicable, does NOT drop overall_passed.
    """
    _node_project_no_coverage(tmp_path)
    _write_manifest(tmp_path)

    cov_result = run_coverage_check(tmp_path, "F001")
    assert cov_result["status"] == "no_signal"
    assert cov_result["passed"] is False

    quality_dir = tmp_path / ".aah" / "build" / "quality-results"
    quality_dir.mkdir(parents=True, exist_ok=True)
    write_json(cov_result, quality_dir / "F001-coverage.json")

    summary = get_quality_summary(tmp_path, "F001")
    assert summary["overall_passed"] is False

    tmp_path2 = tmp_path / "subdir"
    tmp_path2.mkdir()
    _bare_python_project(tmp_path2)
    _write_manifest(tmp_path2)

    tc_result = run_type_check(tmp_path2, "F002")
    assert tc_result["status"] == "not_applicable"

    quality_dir2 = tmp_path2 / ".aah" / "build" / "quality-results"
    quality_dir2.mkdir(parents=True, exist_ok=True)
    write_json(tc_result, quality_dir2 / "F002-type-check.json")

    summary2 = get_quality_summary(tmp_path2, "F002")
    assert summary2["overall_passed"] is True  # not_applicable does not block


def test_summary_has_no_optimistic_default(tmp_path):
    """AC4: write coverage JSON {passed:false,status:"no_signal"} → summary non-passing, overall_passed False;
    ALSO write legacy file with NO status key + passed:false → summary derives non-pass.
    """
    _bare_python_project(tmp_path)
    _write_manifest(tmp_path)
    quality_dir = tmp_path / ".aah" / "build" / "quality-results"
    quality_dir.mkdir(parents=True, exist_ok=True)

    modern = {
        "check": "coverage",
        "feature_id": "F001",
        "passed": False,
        "status": "no_signal",
        "details": {"message": "no signal"},
    }
    write_json(modern, quality_dir / "F001-coverage.json")

    summary1 = get_quality_summary(tmp_path, "F001")
    assert summary1["checks"]["coverage"]["status"] == "no_signal"
    assert summary1["overall_passed"] is False

    legacy = {
        "check": "coverage",
        "feature_id": "F002",
        "passed": False,
        "details": {"message": "legacy artifact"},
    }
    write_json(legacy, quality_dir / "F002-coverage.json")

    summary2 = get_quality_summary(tmp_path, "F002")
    assert summary2["checks"]["coverage"]["status"] == "no_signal"
    assert summary2["overall_passed"] is False




def _init_git_repo(tmp_path: Path) -> None:
    """Initialize git repo with initial commit and attestation secret."""
    builder = AAHProjectBuilder(tmp_path)
    builder.git("init")
    builder.git("config", "user.email", "test@example.com")
    builder.git("config", "user.name", "Test User")
    builder.file("README.md", "# Test").commit("Initial commit").secret()


def _checkpoint_project(
    tmp_path: Path, *, branch: str | None = None
) -> AAHProjectBuilder:
    _init_git_repo(tmp_path)
    builder = AAHProjectBuilder(tmp_path).manifest(
        project_name="test"
    ).waves([["F001"]])
    write_yaml(
        {
            "checkpoint_configuration": {
                "verification_profiles": {
                    "F001": {
                        "level": "standard",
                        "rule_version": "1",
                        "reasons": [],
                        "required_checks": {},
                    }
                }
            }
        },
        builder.aah / "plan" / "checkpoint-config.yaml",
    )
    if branch:
        builder.git("checkout", "-b", branch)
    return builder


def _write_tampered_runtime(tmp_path: Path, wave: int) -> None:
    """Write a runtime result file with invalid attestation (tampered)."""
    runtime_dir = AAHProjectBuilder(tmp_path).dirs("build/runtime-results").aah / "build" / "runtime-results"
    runtime_path = runtime_dir / f"wave-{wave}-all.json"

    payload = {
        "overall_passed": True,
        "summary": {"total_checks": 5, "passed": 5},
    }
    write_attested(
        payload,
        runtime_path,
        project_path=tmp_path,
        command=["aah", "run", "core.build.write_runtime_results", "--wave", str(wave)],
        exit_code=0,
        stdout="",
        stderr="",
        duration_ms=100,
        artifact_name=f"wave-{wave} runtime results",
    )
    data = read_json(runtime_path)
    data["overall_passed"] = False  # tamper
    write_json(data, runtime_path)


def _write_valid_runtime(tmp_path: Path, wave: int, passed: bool, subject_sha: str) -> None:
    """Write a valid attested runtime result."""
    runtime_dir = AAHProjectBuilder(tmp_path).dirs("build/runtime-results").aah / "build" / "runtime-results"
    runtime_path = runtime_dir / f"wave-{wave}-all.json"

    from aah.core.build.verify import runtime_criteria_identity

    payload = {
        "overall_passed": passed,
        "summary": {"total_checks": 5, "passed": 5 if passed else 2},
        "subject": {
            "commit_sha": subject_sha,
            "branch": f"integration/wave-{wave}",
        },
        # Derived locally by the real writer; the consolidation requires it to
        # equal the current value, so seed it the same way.
        "runtime_criteria_sha256": runtime_criteria_identity(tmp_path, wave),
    }
    write_attested(
        payload,
        runtime_path,
        project_path=tmp_path,
        command=["aah", "run", "core.build.write_runtime_results", "--wave", str(wave)],
        exit_code=0 if passed else 1,
        stdout="",
        stderr="",
        duration_ms=100,
        artifact_name=f"wave-{wave} runtime results",
    )


def _write_valid_regression(tmp_path: Path, passed: bool, subject_sha: str, wave: int, status: str = None) -> None:
    """Write a valid attested regression result."""
    test_dir = AAHProjectBuilder(tmp_path).dirs("build/test-results").aah / "build" / "test-results"
    regression_path = test_dir / "regression-latest.json"

    payload = {
        "passed": passed,
        "status": status or ("pass" if passed else "fail"),
        "exit_code": 0 if passed else 1,
        "subject": {
            "commit_sha": subject_sha,
            "branch": f"integration/wave-{wave}",
        },
    }
    write_attested(
        payload,
        regression_path,
        project_path=tmp_path,
        command=["aah", "run", "core.build.run_regression_suite"],
        exit_code=0 if passed else 1,
        stdout="",
        stderr="",
        duration_ms=500,
        artifact_name="regression suite",
    )


def _write_valid_quality(tmp_path: Path, feature_id: str, passed: bool, subject_sha: str) -> None:
    """Write a valid attested quality result."""
    quality_dir = AAHProjectBuilder(tmp_path).dirs("build/quality-results").aah / "build" / "quality-results"
    standards_path = quality_dir / f"{feature_id}-standards.json"

    payload = {
        "feature_id": feature_id,
        "overall_passed": passed,
        "verdict": "PASS" if passed else "BLOCK",
        "subject": {
            "commit_sha": subject_sha,
            "branch": f"feature/{feature_id}",
        },
    }
    write_attested(
        payload,
        standards_path,
        project_path=tmp_path,
        command=["aah", "run", "core.build.quality_checks"],
        exit_code=0 if passed else 1,
        stdout="",
        stderr="",
        duration_ms=200,
        artifact_name=f"{feature_id} standards check",
    )




def test_verifier_tampered_runtime_fails_closed(tmp_path):
    """AC1: tampered runtime evidence can never satisfy the wave.

    Formerly asserted through the validate_checkpoint run-system consolidator.
    That wrapper is gone (issue #117), so the same guarantee is asserted where
    it now lives: the single verifier authenticates the runtime artifact
    directly and refuses it, rather than a second reader repackaging a verdict.
    """
    from aah.core.build.verify import WaveVerificationFailed, verify_wave_evidence

    _checkpoint_project(tmp_path, branch="integration/wave-0")
    integration_sha = subject_identity(tmp_path)

    _write_valid_regression(
        tmp_path, passed=True, subject_sha=integration_sha, wave=0
    )
    _write_tampered_runtime(tmp_path, 0)

    with pytest.raises(WaveVerificationFailed) as exc_info:
        verify_wave_evidence(tmp_path, 0)
    codes = {f.code for f in exc_info.value.report.failures}
    # Fail closed: the tampered payload is treated as not-yet-run, never as the
    # `overall_passed: true` it claims.
    assert "runtime_missing" in codes, codes
    assert "runtime_failed" not in codes


def test_verifier_accepts_valid_runtime_bound_to_the_subject(tmp_path):
    """The positive control: valid, subject-bound runtime evidence is honored.

    Replaces the consolidator's evidence_ref/subject_sha assertions — the
    verifier now reads the signed runtime result directly, so subject binding is
    checked there.
    """
    from aah.core.build.verify import WaveVerificationFailed, verify_wave_evidence

    _checkpoint_project(tmp_path, branch="integration/wave-0")
    integration_sha = subject_identity(tmp_path)

    _write_valid_regression(
        tmp_path, passed=True, subject_sha=integration_sha, wave=0
    )
    _write_valid_runtime(tmp_path, 0, passed=True, subject_sha=integration_sha)

    try:
        verify_wave_evidence(tmp_path, 0)
        failures = []
    except WaveVerificationFailed as exc:
        failures = [f.code for f in exc.report.failures]

    # Runtime and regression are both satisfied; anything still failing belongs
    # to a different gate (features/QA/summary), which this fixture never seeds.
    assert not [c for c in failures if c.startswith("runtime_")], failures
    assert not [c for c in failures if c.startswith("regression_")], failures


def test_verifier_no_signal_regression_blocks(tmp_path):
    """AC4: the sole promotion verifier blocks no-signal regression."""
    from aah.core.build.verify import WaveVerificationFailed, verify_wave_evidence

    builder = _checkpoint_project(tmp_path, branch="integration/wave-0")
    integration_sha = subject_identity(tmp_path)

    _write_valid_regression(tmp_path, passed=False, subject_sha=integration_sha, wave=0, status="no_signal")

    with pytest.raises(WaveVerificationFailed) as exc_info:
        verify_wave_evidence(tmp_path, 0)
    assert any(
        failure.code == "regression_no_signal"
        for failure in exc_info.value.report.failures
    )


def test_verifier_no_valid_regression_but_passing_runtime_blocks(tmp_path):
    """AC3: passing runtime cannot replace missing regression evidence."""
    from aah.core.build.verify import WaveVerificationFailed, verify_wave_evidence

    builder = _checkpoint_project(tmp_path, branch="integration/wave-0")
    integration_sha = builder.git("rev-parse", "HEAD")

    _write_valid_runtime(tmp_path, 0, passed=True, subject_sha=integration_sha)


    with pytest.raises(WaveVerificationFailed) as exc_info:
        verify_wave_evidence(tmp_path, 0)
    assert any(
        failure.code == "regression_missing"
        for failure in exc_info.value.report.failures
    )


def test_verifier_human_review_required_without_approval_blocks(tmp_path):
    """AC5: the sole promotion verifier requires the human decision."""
    from aah.core.build.verify import WaveVerificationFailed, verify_wave_evidence

    builder = _checkpoint_project(tmp_path, branch="integration/wave-0")
    integration_sha = subject_identity(tmp_path)

    progress_path = tmp_path / ".aah" / "claude-progress.json"
    write_json({"current_wave": 0}, progress_path)

    _write_valid_regression(tmp_path, passed=True, subject_sha=integration_sha, wave=0)

    seed_qa_attempt(
        tmp_path, "F001", 1, "human_review_required", integration_sha
    )

    with pytest.raises(WaveVerificationFailed) as exc_info:
        verify_wave_evidence(tmp_path, 0)
    assert any(
        failure.feature_id == "F001"
        and failure.action == "human_review_required"
        for failure in exc_info.value.report.failures
    )


def test_promote_human_review_with_approval_proceeds(tmp_path):
    """AC5: promote with feature QA pass verdict → _feature_qa_final_approved returns True."""
    from aah.core.build.qa_routing import _feature_qa_final_approved

    builder = _checkpoint_project(tmp_path, branch="integration/wave-0")
    builder.waves([{"features": [["F001"]]}])
    aah_path = builder.aah
    subject_sha = subject_identity(tmp_path)

    progress_path = aah_path / "claude-progress.json"
    write_json({"current_wave": 0}, progress_path)

    seed_qa_attempt(tmp_path, "F001", 1, "pass", subject_sha)

    approved = _feature_qa_final_approved(tmp_path, aah_path, "F001")
    assert approved is True, "Feature should be approved with passing QA verdict"


def _rotate_secret(tmp_path: Path) -> None:
    """Rotate the attestation secret (simulates cross-session resume)."""
    import secrets
    secret_path = tmp_path / ".aah" / "build" / ".attestation-secret"
    secret_path.write_bytes(secrets.token_bytes(32))


def test_regression_reverify_on_stale_secret_then_advances(tmp_path):
    """AC1/AC2: write attested regression → rotate secret → reverify detection
    detects stale secret → reverify action emitted."""
    from aah.core.build.orchestrator import _reverify_action
    from aah.core.build.verify import read_regression_evidence

    builder = _checkpoint_project(tmp_path, branch="integration/wave-0")
    subject_sha = subject_identity(tmp_path)

    _write_valid_regression(tmp_path, passed=True, subject_sha=subject_sha, wave=0)

    fresh, reason1 = read_regression_evidence(builder.aah, tmp_path, 0)
    assert fresh is not None
    assert reason1 == "", "Fresh evidence should not trigger reverify"

    _rotate_secret(tmp_path)

    stale, reason2 = read_regression_evidence(builder.aah, tmp_path, 0)
    assert stale is None
    assert reason2 == "stale_secret"

    action = _reverify_action(
        "regression",
        command="aah run core.build.run_regression_suite --wave 0",
        reason=reason2,
        wave=0,
    )
    assert action["action"] == "reverify_evidence"
    assert action["evidence_type"] == "regression"
    assert "run_regression_suite" in action["command"]

    _write_valid_regression(tmp_path, passed=True, subject_sha=subject_sha, wave=0)

    refreshed, reason3 = read_regression_evidence(builder.aah, tmp_path, 0)
    assert refreshed is not None
    assert reason3 == "", "After re-run with fresh secret, should not reverify"


def test_tampered_regression_not_downgraded_to_reverify(tmp_path):
    """AC2 anti-downgrade: tamper regression file → NOT reverify, must be refuse."""
    from aah.core.build.verify import read_regression_evidence

    builder = _checkpoint_project(tmp_path)
    subject_sha = builder.git("rev-parse", "HEAD")

    test_dir = builder.dirs("build/test-results").aah / "build" / "test-results"
    _write_valid_regression(tmp_path, passed=True, subject_sha=subject_sha, wave=0)
    reg_path = test_dir / "regression-latest.json"
    data = read_json(reg_path)
    data["passed"] = False  # Tamper after signing
    write_json(data, reg_path)

    regression, reason = read_regression_evidence(builder.aah, tmp_path, 0)
    assert regression is None
    assert reason != "stale_secret"


def test_refused_feature_test_with_human_approve_does_not_complete_wave(tmp_path):
    """AC4: feature with refused/no_signal feature-test gate + fresh attested
    human-approve for current SHA → _feature_qa_final_approved still requires
    valid feature test (human approve can't override)."""
    from aah.core.build.qa_routing import _feature_qa_final_approved

    builder = _checkpoint_project(tmp_path)
    subject_sha = subject_identity(tmp_path)
    write_json({"current_wave": 0, "current_phase": "build"}, tmp_path / ".aah" / "claude-progress.json")

    test_dir = builder.dirs("build/test-results").aah / "build" / "test-results"
    write_attested(
        {"feature_id": "F001", "passed": False, "status": "no_signal"},
        test_dir / "F001.json",
        project_path=tmp_path,
        command=["aah", "run", "core.build.run_feature_tests"],
        exit_code=1, stdout="", stderr="", duration_ms=100,
    )

    qa_dir = builder.dirs("build/qa-results/F001/human-review").aah / "build" / "qa-results" / "F001" / "human-review"
    human_payload = {
        "feature_id": "F001",
        "decision": "approve",
        "subject": {
            "commit_sha": subject_sha,
            "branch": "main",
        },
    }
    write_attested(
        human_payload,
        qa_dir / "human-001.json",
        project_path=tmp_path,
        command=["aah", "run", "core.build.write_qa_report", "--human-decision"],
        exit_code=0, stdout="", stderr="", duration_ms=50,
    )

    approved = _feature_qa_final_approved(tmp_path, tmp_path / ".aah", "F001")
    assert approved is False, "Human approve should NOT override refused/no_signal feature test gate"


def test_sha_aware_enqueue_stale_approval_reenters_qa(tmp_path):
    """SHA-aware enqueue (closed MEDIUM): feature with QA pass bound to SHA-A,
    then feature_qa_final_approved checks with SHA-B (v2 on) → returns False."""
    from aah.core.build.qa_routing import (
        _feature_qa_final_approved,
        _features_needing_qa,
    )

    builder = _checkpoint_project(tmp_path)
    sha_a = subject_identity(tmp_path)
    write_json({"current_wave": 0, "current_phase": "build"}, tmp_path / ".aah" / "claude-progress.json")

    test_dir = builder.dirs("build/test-results").aah / "build" / "test-results"
    write_attested(
        {"feature_id": "F001", "passed": True, "subject": {"commit_sha": sha_a, "branch": "main"}},
        test_dir / "F001.json",
        project_path=tmp_path,
        command=["aah", "run", "core.build.run_feature_tests"],
        exit_code=0, stdout="", stderr="", duration_ms=100,
    )

    seed_qa_attempt(tmp_path, "F001", 1, "pass", sha_a)

    approved_at_a = _feature_qa_final_approved(tmp_path, tmp_path / ".aah", "F001")
    assert approved_at_a is True, "Feature should be approved at SHA-A"

    builder.file("change.txt", "new content").commit("new commit")
    sha_b = subject_identity(tmp_path)

    approved_at_b = _feature_qa_final_approved(tmp_path, tmp_path / ".aah", "F001")
    assert approved_at_b is False, "Feature should NOT be approved at SHA-B with stale SHA-A QA approval"

    write_attested(
        {"feature_id": "F001", "passed": True, "subject": {"commit_sha": sha_b, "branch": "main"}},
        test_dir / "F001.json",
        project_path=tmp_path,
        command=["aah", "run", "core.build.run_feature_tests"],
        exit_code=0, stdout="", stderr="", duration_ms=100,
    )

    needing_qa = _features_needing_qa(tmp_path, tmp_path / ".aah", ["F001"])
    assert "F001" in needing_qa, "F001 should need QA when approval is for stale SHA"
