"""Merge-boundary attestation tests (consumer side).

The attestation boundary uses real files, real HMAC signatures, and real key
replacement. The promotion success case isolates the later full-wave verifier,
which is covered independently, so this module stays focused on Git gates.
"""

import os
import secrets
import subprocess

import pytest

from aah.core.common.attestation import write_attested
from aah.core.common.git_utils import checkout_branch, create_branch
from aah.core.common.io_utils import read_json, write_json, write_yaml
from aah.core.git_ops.merge_wave_to_integration import merge_wave_to_integration
from aah.core.git_ops.promote_to_develop import promote_to_develop
from aah.core.build.verify import REGRESSION_PREFIX, RUNTIME_RESULTS_PREFIX


REGRESSION_REL = "build/test-results/regression-latest.json"


def _runtime_rel(wave):
    return f"build/runtime-results/wave-{wave}-all.json"


@pytest.fixture(autouse=True)
def git_env():
    for k, v in {
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@test.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@test.com",
    }.items():
        os.environ[k] = v
    yield


@pytest.fixture
def gate_project(tmp_path):
    """Git repo + minimal .aah/ with a seeded attestation secret.

    A real repo with develop + integration/wave-0 lets the promote gate reach
    its attestation check (it verifies branch existence first). The merge gate
    runs its attestation check before any git work regardless.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=proj, capture_output=True, check=True)
    (proj / "README.md").write_text("# t\n")
    subprocess.run(["git", "add", "-A"], cwd=proj, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=proj, capture_output=True, check=True)
    create_branch("develop", cwd=proj)
    create_branch("integration/wave-0", cwd=proj)
    checkout_branch("main", cwd=proj)

    rapids = proj / ".aah"
    for d in ["plan", "build/test-results", "build/runtime-results"]:
        (rapids / d).mkdir(parents=True, exist_ok=True)
    write_yaml({
        "project_name": "test",
        "branching_config": {"develop_branch": "develop", "integration_prefix": "integration/wave-"},
    }, rapids / "manifest.yaml")
    write_json({"waves": [["F001"]], "total_waves": 1}, rapids / "plan" / "waves.json")
    (rapids / "build" / ".attestation-secret").write_bytes(secrets.token_bytes(32))
    return proj


def _attest(project, rel, payload, prefix):
    """Write a payload with a real attestation block under `prefix`."""
    path = project / ".aah" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    write_attested(
        payload, path, project_path=project, command=prefix,
        exit_code=0, stdout="", stderr="", duration_ms=1,
    )
    return path


def _rotate_secret(project):
    (project / ".aah" / "build" / ".attestation-secret").write_bytes(secrets.token_bytes(32))


def _pass_runtime(project, wave=0):
    _attest(project, _runtime_rel(wave), {"overall_passed": True}, RUNTIME_RESULTS_PREFIX)


def _pass_regression(project, subject=None):
    """Write a regression result with optional subject binding."""
    payload = {"passed": True, "status": "pass"}
    if subject is not None:
        payload["subject"] = subject
    _attest(project, REGRESSION_REL, payload, REGRESSION_PREFIX)


def _snapshot_refs(project):
    """Capture current branch refs for before/after comparison."""
    from aah.core.common.git_utils import rev_parse
    return {
        "develop": rev_parse("develop", cwd=project),
        "main": rev_parse("main", cwd=project),
        "integration/wave-0": rev_parse("integration/wave-0", cwd=project),
    }


def _read_audit(project):
    """Read attestation gate audit log."""
    audit_path = project / ".aah" / "audit" / "attestation-gate-log.json"
    if not audit_path.exists():
        return {"events": []}
    return read_json(audit_path)


# ── AC1: tampered → refuse ────────────────────────────────────────────

def test_tampered_runtime_refused(gate_project):
    path = _attest(gate_project, _runtime_rel(0), {"overall_passed": True}, RUNTIME_RESULTS_PREFIX)
    data = read_json(path)
    data["overall_passed"] = False
    data["extra"] = "tamper"
    write_json(data, path)

    result = merge_wave_to_integration(gate_project, 0)
    assert result["success"] is False
    assert "attestation failed" in result["error"]


# ── AC2: missing / bad-prefix / no-attestation / malformed → refuse ───

def test_no_attestation_block_refused(gate_project):
    _pass_runtime(gate_project)
    write_json({"passed": True, "status": "pass"}, gate_project / ".aah" / REGRESSION_REL)

    result = merge_wave_to_integration(gate_project, 0)
    assert result["success"] is False
    assert "attestation failed" in result["error"]


def test_truncated_json_refused(gate_project):
    _pass_runtime(gate_project)
    (gate_project / ".aah" / REGRESSION_REL).write_text('{"passed": true, ')

    result = merge_wave_to_integration(gate_project, 0)
    assert result["success"] is False
    # unparseable file → attestation-failure path (not the missing-file msg)
    assert "attestation failed" in result["error"]


def test_absent_regression_file(gate_project):
    _pass_runtime(gate_project)
    # no regression file at all
    result = merge_wave_to_integration(gate_project, 0)
    assert result["success"] is False
    assert "regression results missing" in result["error"]


# ── AC3: no_signal → not-green, no crash ──────────────────────────────

def test_no_signal_regression_blocks(gate_project):
    _pass_runtime(gate_project)
    _attest(gate_project, REGRESSION_REL,
            {"passed": False, "status": "no_signal"}, REGRESSION_PREFIX)

    result = merge_wave_to_integration(gate_project, 0)  # must not raise
    assert result["success"] is False
    assert "no signal" in result["error"]


# ── AC1: stale secret → reverify_required + block ────────────────────

def test_stale_secret_reverify_required_blocks(gate_project):
    """After secret rotation, merge/promote must block with reverify_required,
    instructing re-run of the affected gate. Refs unchanged."""
    refs_before = _snapshot_refs(gate_project)
    _pass_runtime(gate_project)
    _pass_regression(gate_project)
    _rotate_secret(gate_project)

    result = merge_wave_to_integration(gate_project, 0)
    assert result["success"] is False
    assert "reverif" in result["error"].lower()
    refs_after = _snapshot_refs(gate_project)
    assert refs_before == refs_after
    audit = _read_audit(gate_project)
    assert any(e["verdict"] == "reverify_required" for e in audit["events"])


def test_stale_no_signal_reverify_first(gate_project):
    """reverify_required verdict returns BEFORE no_signal read — still blocks."""
    _pass_runtime(gate_project)
    _attest(gate_project, REGRESSION_REL,
            {"passed": True, "status": "no_signal"}, REGRESSION_PREFIX)
    _rotate_secret(gate_project)

    result = merge_wave_to_integration(gate_project, 0)
    assert result["success"] is False
    # reverify_required takes precedence over no_signal check
    assert "reverif" in result["error"].lower()


# ── AC2: tamper after rotation still blocks ──────────────────────────

def test_tamper_after_rotation_still_blocked(gate_project):
    """Closes the downgrade gap: tampering AFTER rotation is now blocked
    (data is None on reverify_required verdict, so caller cannot read plain fields).
    This test documents the gap closure — old behavior allowed this tamper through
    via downgrade; new behavior blocks it."""
    refs_before = _snapshot_refs(gate_project)
    _pass_runtime(gate_project)
    path = _attest(gate_project, REGRESSION_REL, {"passed": True, "status": "pass"}, REGRESSION_PREFIX)
    data = read_json(path)
    data["passed"] = False  # tamper
    write_json(data, path)
    _rotate_secret(gate_project)  # THEN rotate

    result = merge_wave_to_integration(gate_project, 0)
    # NOW blocks (no longer downgrades) — gap closed
    assert result["success"] is False
    refs_after = _snapshot_refs(gate_project)
    assert refs_before == refs_after
    audit = _read_audit(gate_project)
    assert any(e["verdict"] == "reverify_required" for e in audit["events"])


# ── AC4: refused + audited ────────────────────────────────────────────

def test_tampered_regression_refused_and_audited(gate_project):
    """Tampered regression → refuse verdict + audit event."""
    _pass_runtime(gate_project)
    path = _attest(gate_project, REGRESSION_REL, {"passed": True, "status": "pass"}, REGRESSION_PREFIX)
    data = read_json(path)
    data["passed"] = False  # mutate signed field
    write_json(data, path)

    result = merge_wave_to_integration(gate_project, 0)
    assert result["success"] is False
    assert "attestation failed" in result["error"]
    audit = _read_audit(gate_project)
    events = [e for e in audit["events"] if e["verdict"] == "refuse"]
    assert len(events) > 0
    assert any("signature_mismatch" in e.get("reason", "") for e in events)


def test_wrong_prefix_refused_and_audited(gate_project):
    """Wrong command prefix → refuse + audit."""
    _pass_runtime(gate_project)
    _attest(gate_project, REGRESSION_REL, {"passed": True, "status": "pass"},
            ["aah", "run", "core.build.other"])

    result = merge_wave_to_integration(gate_project, 0)
    assert result["success"] is False
    audit = _read_audit(gate_project)
    events = [e for e in audit["events"] if e["verdict"] == "refuse"]
    assert len(events) > 0
    assert any("command_mismatch" in e.get("reason", "") for e in events)


def test_malformed_attestation_refused_and_audited(gate_project):
    """Malformed attestation (missing required field) → refuse + audit."""
    _pass_runtime(gate_project)
    path = _attest(gate_project, REGRESSION_REL, {"passed": True, "status": "pass"}, REGRESSION_PREFIX)
    data = read_json(path)
    del data["attestation"]["host_pid"]  # remove required field
    write_json(data, path)

    result = merge_wave_to_integration(gate_project, 0)
    assert result["success"] is False
    audit = _read_audit(gate_project)
    events = [e for e in audit["events"] if e["verdict"] == "refuse"]
    assert len(events) > 0


def test_wrong_sha_subject_refused_and_audited(gate_project):
    """Regression with wrong subject SHA → refuse + audit with expected/recorded."""
    from aah.core.common.git_utils import rev_parse
    _pass_runtime(gate_project)
    # Write regression with wrong SHA
    wrong_subject = {"branch": "integration/wave-0", "commit_sha": "0" * 40}
    _pass_regression(gate_project, subject=wrong_subject)

    result = merge_wave_to_integration(gate_project, 0)
    assert result["success"] is False
    assert "SHA mismatch" in result["error"]
    audit = _read_audit(gate_project)
    events = [e for e in audit["events"] if e["verdict"] == "refuse" and "sha_mismatch" in e.get("reason", "")]
    assert len(events) > 0
    # Verify audit captured expected vs recorded
    event = events[0]
    assert "expected_subject" in event
    assert "recorded_subject" in event
    assert event["expected_subject"]["commit_sha"] != "0" * 40
    assert event["recorded_subject"]["commit_sha"] == "0" * 40


# ── AC5: fresh pipeline merges and promotes ──────────────────────────

def test_fresh_pipeline_merges(gate_project):
    """Real repo: fresh runtime + regression with correct subject → merge succeeds."""
    from aah.core.common.git_utils import rev_parse, run_git
    from aah.core.common.attestation import write_attested
    import subprocess

    # Commit a feature on develop
    checkout_branch("develop", cwd=gate_project)
    (gate_project / "feat.txt").write_text("F001\n")
    subprocess.run(["git", "add", "feat.txt"], cwd=gate_project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feat(F001): implement"], cwd=gate_project, check=True, capture_output=True)

    # Recreate integration/wave-0 from develop (delete existing, then create from develop)
    run_git(["branch", "-D", "integration/wave-0"], cwd=gate_project, check=False)
    create_branch("integration/wave-0", cwd=gate_project)
    checkout_branch("integration/wave-0", cwd=gate_project)

    # Get current HEAD
    head = rev_parse("integration/wave-0", cwd=gate_project)
    assert head is not None

    # Write required feature artifacts so merge validation passes
    aah = gate_project / ".aah"
    write_attested(
        {"passed": True}, aah / "build" / "test-results" / "F001.json",
        project_path=gate_project, command=["aah", "run", "core.build.x"],
        exit_code=0, stdout="", stderr="", duration_ms=1,
    )
    from aah.core.build.verify import subject_identity
    from tests._qa_helpers import seed_qa_attempt

    seed_qa_attempt(
        gate_project, "F001", 1, "pass", subject_identity(gate_project)
    )
    write_attested(
        {"passed": True}, aah / "build" / "quality-results" / "F001-standards.json",
        project_path=gate_project, command=["aah", "run", "core.build.x"],
        exit_code=0, stdout="", stderr="", duration_ms=1,
    )

    # Write runtime result
    _pass_runtime(gate_project)

    # Write regression with correct subject
    subject = {
        "branch": "integration/wave-0",
        "commit_sha": subject_identity(gate_project),
    }
    _pass_regression(gate_project, subject=subject)

    # Merge should succeed
    result = merge_wave_to_integration(gate_project, 0)
    assert result["success"] is True, result
    # integration branch should contain F001 commit
    integration_head = rev_parse("integration/wave-0", cwd=gate_project)
    log_result = run_git(["log", integration_head, "--oneline", "--grep=F001", "-1"], cwd=gate_project, check=False)
    assert "F001" in log_result.stdout
    # No reverify/refuse events in audit
    audit = _read_audit(gate_project)
    bad_events = [e for e in audit["events"] if e["verdict"] in ("reverify_required", "refuse")]
    assert len(bad_events) == 0


def test_fresh_pipeline_promotes(gate_project, monkeypatch):
    """Fresh regression with correct subject → promote succeeds, develop advances."""
    from aah.core.build.verify import subject_identity
    from aah.core.common.git_utils import rev_parse, is_ancestor, run_git
    from tests._qa_helpers import seed_qa_attempt
    import subprocess

    # Commit a feature on develop (use F001 to match waves.json fixture)
    checkout_branch("develop", cwd=gate_project)
    (gate_project / "feat.txt").write_text("F001\n")
    subprocess.run(["git", "add", "feat.txt"], cwd=gate_project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feat(F001): implement"], cwd=gate_project, check=True, capture_output=True)

    # Recreate integration/wave-0 from develop (delete existing, then create from develop)
    run_git(["branch", "-D", "integration/wave-0"], cwd=gate_project, check=False)
    create_branch("integration/wave-0", cwd=gate_project)
    checkout_branch("integration/wave-0", cwd=gate_project)
    integration_head = rev_parse("integration/wave-0", cwd=gate_project)

    # Write fresh regression with correct subject (promote only needs regression, not artifacts)
    current_subject = subject_identity(gate_project)
    subject = {"branch": "integration/wave-0", "commit_sha": current_subject}
    _pass_regression(gate_project, subject=subject)
    write_json(
        {"current_wave": 0, "current_phase": "build"},
        gate_project / ".aah" / "claude-progress.json",
    )
    seed_qa_attempt(gate_project, "F001", 1, "pass", current_subject)

    # This test owns promotion's regression/QA/Git boundary. The full evidence
    # verifier has its own end-to-end suite and otherwise requires every wave
    # artifact, obscuring the boundary exercised here.
    import aah.core.build.verify as verify_mod
    from aah.core.build.verify import VerificationReport

    passing = VerificationReport(
        wave=0,
        passed=True,
        failures=(),
        subject_identity=current_subject,
        runtime_criteria_sha256="test-criteria",
        artifact_sha256={},
    )
    monkeypatch.setattr(verify_mod, "verify_wave_evidence", lambda p, w: passing)
    monkeypatch.setattr(
        verify_mod, "verification_report_is_current", lambda p, r: (True, "")
    )

    develop_before = rev_parse("develop", cwd=gate_project)
    result = promote_to_develop(gate_project, 0)
    assert result["success"] is True, result
    develop_after = rev_parse("develop", cwd=gate_project)
    # develop should have advanced
    assert develop_after != develop_before
    # develop should contain integration head
    assert is_ancestor(integration_head, "develop", cwd=gate_project)
    # No bad audit events
    audit = _read_audit(gate_project)
    bad_events = [e for e in audit["events"] if e["verdict"] in ("reverify_required", "refuse")]
    assert len(bad_events) == 0
