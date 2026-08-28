"""Real-project tests for the orchestrator runtime-profile gate.

They pin required-check enforcement, post-signature tamper rejection, removal
of keyword heuristics, and report-only/enforce/off rollout behavior.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path

from tests.support.aah_project import AAHProjectBuilder

from aah.core.build.orchestrator import (
    _runtime_profile_gate,
    _runtime_profile_rollout,
    compute_next_action,
)
from aah.core.build.verify import (
    RUNTIME_RESULTS_PREFIX,
    RUNTIME_RESULTS_PREFIXES,
    read_attested,
)
from aah.core.common.attestation import write_attested
from aah.core.common.io_utils import read_json, write_json, write_yaml
from aah.core.common.progress import get_default_progress, save_progress
from aah.core.common.git_utils import rev_parse
from tests._runtime_helpers import (
    confirm_profile_cli,
    make_project,
    read_attested_wave,
)


def _write_runtime_results(project: Path, wave: int = 0) -> None:
    """Produce a real PASSING runtime result through the real writer CLI.

    verify.py no longer executes anything, so the evidence under test has to
    come from its actual producer. The writer derives overall_passed, the
    subject, and runtime_criteria_sha256 itself.
    """
    import json as _json
    from aah.core.build.verification_identity import (
        resolve_runtime_verification_profile,
    )

    profile, _ = resolve_runtime_verification_profile(project)
    checks = {
        key: {
            "check": key,
            "wave": wave,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "passed": True,
            "details": {"message": "ok"},
        }
        for key in ("module_validation", "startup_validation", "smoke_tests", "health_check")
    }
    result = AAHProjectBuilder(project).run_module(
        "aah.core.build.write_runtime_results",
        "--wave", str(wave),
        "--results-json", _json.dumps(checks),
        "--project-path", str(project),
    )
    assert result.returncode == 0, result.stderr

    # The gate reads runtime_evidence (profile bindings + required_checks),
    # which the agent-facing writer does not emit. Layer it on and re-sign so
    # the artifact matches what the enforced gate consumes.
    path = project / ".aah" / "build" / "runtime-results" / f"wave-{wave}-all.json"
    payload = _json.loads(path.read_text())
    payload["checks"].update({
        key: {"check": key, "passed": True, "skipped": False}
        for key in ("build", "server_start", "smoke_tests", "cloud_readiness")
    })
    payload["runtime_evidence"] = {
        "integration_sha": payload["subject"]["commit_sha"],
        "profile_bindings": {"profile_hash": (profile or {}).get("profile_hash")},
        "required_checks": list((profile or {}).get("required_checks") or []),
    }
    _re_sign(project, path, payload)

# ``write_runtime_results`` is the only current writer. Readers also accept
# the former ``core.build.verify`` signature for stored migration evidence.
_RUNTIME_WRITER_CMD = ["aah", "run", "core.build.write_runtime_results"]
def _git(project: Path, *args: str) -> str:
    return AAHProjectBuilder(project).git(*args)


def _init_git(project: Path) -> None:
    builder = AAHProjectBuilder(project)
    builder.git("init", "-b", "main")
    builder.git("config", "user.name", "T")
    builder.git("config", "user.email", "t@t")


def _real_wave_project(tmp_path: Path, *, runtime_profile_v1) -> Path:
    """A real project on a real git repo with a real integration/wave-0 branch
    and a real confirmed runtime profile. Returns the project root."""
    project = make_project(
        tmp_path,
        topology="full-local",
        transport="localhost",
        runtime_profile_v1=runtime_profile_v1,
        with_cloud_readiness=False,
    )
    _init_git(project)
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "init")
    _git(project, "branch", "integration/wave-0")
    _git(project, "checkout", "integration/wave-0")
    confirm_profile_cli(project)
    return project


def _re_sign(project: Path, path: Path, payload: dict) -> None:
    """Re-attest a mutated payload with the real runtime-writer prefix."""
    payload.pop("attestation", None)
    write_attested(
        payload,
        path,
        project_path=project,
        command=_RUNTIME_WRITER_CMD + ["--wave", "0"],
        exit_code=0,
        stdout="",
        stderr="",
        duration_ms=1,
    )


def _write_artifact(project: Path, relative_path: str, payload: dict, module: str) -> None:
    write_attested(
        payload,
        project / ".aah" / relative_path,
        project_path=project,
        command=["aah", "run", f"core.build.{module}"],
        exit_code=0,
        stdout="",
        stderr="",
        duration_ms=1,
    )


def test_missing_required_check_blocks(tmp_path):
    """AC1: enforce + confirmed + a genuine verify pass, then delete
    checks["build"] and RE-SIGN → the gate blocks with a user_confirm action
    whose reason names required_check_absent_or_failed:build."""
    project = _real_wave_project(tmp_path, runtime_profile_v1=True)
    aah_path = project / ".aah"

    _write_runtime_results(project, 0)
    data = read_attested_wave(project, 0)
    assert data["overall_passed"] is True
    assert "build" in data["runtime_evidence"]["required_checks"]

    rt_path = aah_path / "build" / "runtime-results" / "wave-0-all.json"
    data["checks"].pop("build", None)
    _re_sign(project, rt_path, data)

    resigned = read_attested_wave(project, 0)
    gate = _runtime_profile_gate(project, aah_path, 0, resigned)
    assert gate is not None
    assert gate["action"] == "user_confirm"
    assert gate["runtime_profile_state"] == "enforce"
    assert "required_check_absent_or_failed:build" in gate["reason"]
    assert "required_check_absent_or_failed" in gate["failures"]


def _seed_completed_wave(project: Path) -> Path:
    """Seed all upstream attested gates + a real confirmed profile so
    compute_next_action / _run_l1_verify reaches the runtime-results read.
    Returns the integration/wave-0 HEAD sha."""
    aah = project / ".aah"
    AAHProjectBuilder(project).dirs(
        "build/test-results",
        "build/quality-results",
        "build/checkpoint-results",
        "build/validation-results",
        "audit",
    ).waves([["F001"]])

    prog = get_default_progress()
    prog["current_wave"] = 0
    prog["current_phase"] = "build"
    save_progress(prog, aah / "claude-progress.json")
    write_json(
        {"features": [{"id": "F001", "passes": True, "dependencies": [],
                       "spec_ref": "S1", "description": "A"}]},
        aah / "feature-list.json",
    )
    write_yaml(
        {
            "checkpoint_configuration": {
                "verification_profiles": {
                    "F001": {
                        "level": "standard",
                        "rule_version": "test",
                        "reasons": ["runtime gate fixture"],
                        "required_checks": {},
                    }
                }
            }
        },
        aah / "plan" / "checkpoint-config.yaml",
    )
    AAHProjectBuilder(project).feature(
        "F001",
        frontmatter={
            "description": "x",
            "dependencies": [],
            "acceptance_criteria": [{"id": "AC1", "description": "passes"}],
            "test_cases": [{"id": "TC1", "covers": ["AC1"]}],
        },
        body="",
    )

    _git(project, "add", "-A")
    _git(project, "commit", "-m", "F001: implement")
    _git(project, "branch", "-f", "integration/wave-0", "HEAD")
    _git(project, "checkout", "integration/wave-0")
    head = rev_parse("integration/wave-0", cwd=project)

    from aah.core.build.evidence import (
        build_evidence_v2_record,
        capture_subject,
        hash_feature_contract,
        hash_test_inputs,
    )

    feature_path = aah / "plan" / "features" / "F001.md"
    feature_result = build_evidence_v2_record(
        feature_id="F001",
        producer="run_feature_tests",
        subject=capture_subject(project, project),
        contract_hash=hash_feature_contract(feature_path),
        test_input_hash=hash_test_inputs([feature_path], project),
        test_paths=[".aah/plan/features/F001.md"],
        execution={"argv": ["pytest"], "cwd": ".", "exit_code": 0},
        status="pass",
        artifacts={},
    )
    feature_result.update(
        {"passed": True, "summary": {"total": 1, "passed": 1, "failed": 0},
         "failures": []}
    )
    _write_artifact(
        project, "build/test-results/F001.json", feature_result,
        "run_feature_tests",
    )
    from aah.core.build.verify import subject_identity
    from tests._qa_helpers import seed_qa_attempt

    seed_qa_attempt(project, "F001", 1, "pass", subject_identity(project))
    # Standards is project-scoped and wave-keyed — it must satisfy the last-wave
    # gate, which sits BEFORE regression and the runtime gate under test here.
    _write_artifact(
        project, "build/quality-results/wave-0-project-standards.json",
        {
            "scope": "project", "wave": 0,
            "overall_passed": True, "verdict": "PASS", "status": "pass",
            "subject": {
                "branch": "integration/wave-0",
                "commit_sha": subject_identity(project),
            },
        },
        "quality_checks",
    )
    # The regression reader compares the v2 content identity (the
    # .aah/.claude-excluding tree hash), NOT the raw git SHA — bind to the same
    # thing the reader checks, or the regression gate fires first and the
    # runtime gate under test is never reached.
    _write_artifact(
        project, "build/test-results/regression-latest.json",
        {"status": "pass", "passed": True,
         "summary": {"total": 1, "passed": 1, "failed": 0},
         "failures": [],
         "subject": {"branch": "integration/wave-0",
                     "commit_sha": subject_identity(project)}},
        "run_regression_suite",
    )
    write_json(
        {"feature_id": "F001", "passed": True, "criteria_results": []},
        aah / "build/validation-results/F001-spec-validation.json",
    )
    _write_artifact(
        project, "build/checkpoint-results/wave-0-system-checkpoint.json",
        {"checkpoint_type": "system", "wave": 0, "overall_passed": True,
         "subject": {
             "branch": "integration/wave-0",
             "commit_sha": subject_identity(project),
         },
         "checks": {}, "blocking_issues": []},
        "validate_checkpoint",
    )
    return head


def _seed_runtime_pass(project: Path, head: str) -> Path:
    """Write a real attested passing runtime result bound to the real profile
    + integration subject. Returns the runtime-results path.

    ``head`` is accepted for call-site symmetry but the binding uses the v2
    content identity, which is what _runtime_profile_gate compares.
    """
    from aah.core.build.verification_identity import (
        resolve_runtime_verification_profile,
        runtime_criteria_identity,
        subject_identity,
    )

    aah = project / ".aah"
    profile, _ = resolve_runtime_verification_profile(project)
    bindings = {
        "profile_hash": profile["profile_hash"],
        "readiness_hash": profile["readiness_hash"],
        "decision_hash": profile["decision_hash"],
        "deployment_topology": profile["deployment_topology"],
        "required_checks": profile["required_checks"],
    }
    step_key = {"build": "build", "boot": "server_start", "smoke": "smoke_tests",
                "cloud_readiness": "cloud_readiness"}
    checks = {
        step_key.get(c, c): {"passed": True, "skipped": True}
        for c in profile["required_checks"]
    }
    runtime = {
        "wave": 0, "overall_passed": True, "runtime_mode": "no-runtime", "port": 8000,
        "subject": {
            "branch": "integration/wave-0",
            "commit_sha": subject_identity(project),
        },
        "runtime_criteria_sha256": runtime_criteria_identity(project, 0),
        "checks": checks,
        "summary": {"total_checks": len(checks), "passed": len(checks),
                    "failed": 0, "skipped": len(checks)},
        "runtime_evidence": {
            "runtime_profile_v1": "True", "enforced": True,
            "integration_sha": subject_identity(project),
            "profile_bindings": bindings,
            "required_checks": profile["required_checks"],
        },
    }
    rt = aah / "build/runtime-results/wave-0-all.json"
    write_attested(
        runtime, rt, project_path=project,
        command=_RUNTIME_WRITER_CMD + ["--wave", "0"],
        exit_code=0, stdout="", stderr="", duration_ms=1,
    )
    return rt


def _pin_mtimes(project: Path) -> None:
    aah = project / ".aah"
    base = 1_700_000_000
    os.utime(aah / "build/test-results/regression-latest.json", (base, base))
    os.utime(aah / "build/checkpoint-results/wave-0-system-checkpoint.json",
             (base + 60, base + 60))
    os.utime(aah / "build/runtime-results/wave-0-all.json", (base + 90, base + 90))


def test_post_signature_block_rejected(tmp_path, monkeypatch):
    """AC2: an unsigned post-signature mutation re-runs the checkpoint."""
    monkeypatch.setenv("AAH_VERIFY_INTERNAL", "0")
    project = make_project(
        tmp_path, topology="full-local", transport="localhost",
        runtime_profile_v1=True, with_cloud_readiness=False,
    )
    _init_git(project)
    head = _seed_completed_wave(project)
    confirm_profile_cli(project)
    rt = _seed_runtime_pass(project, head)
    legacy_payload = read_json(rt)
    legacy_payload.pop("attestation")
    write_attested(
        legacy_payload,
        rt,
        project_path=project,
        command=["aah", "run", "core.build.verify", "--wave", "0"],
        exit_code=0,
        stdout="",
        stderr="",
        duration_ms=1,
    )
    _pin_mtimes(project)

    clean = compute_next_action(project)
    assert clean["action"] not in ("run_system_checkpoint", "fix_runtime_validation",
                                   "raise_feedback", "user_confirm"), clean

    data = read_json(rt)
    data["runtime_evidence"]["integration_sha"] = "0" * 40
    write_json(data, rt)
    os.utime(rt, (1_700_000_120, 1_700_000_120))
    assert read_attested(rt, project, RUNTIME_RESULTS_PREFIXES) is None

    mutated = compute_next_action(project)
    assert mutated["action"] == "run_system_checkpoint", mutated
    assert "missing or unverifiable" in mutated.get("reason", "")


def test_required_checks_drive_gate_not_keywords(tmp_path):
    """AC3: only profile-required checks drive the gate."""
    from aah.core.build import orchestrator as orch
    from aah.core.build.verification_identity import runtime_profile_evidence_validation

    src = inspect.getsource(orch._runtime_profile_gate)
    validator_src = inspect.getsource(runtime_profile_evidence_validation)

    assert "_synthesis_declares_services" not in src
    assert "declares_services" not in src
    for banned in (
        "endswith(", "in description", "feature_data", "synthesis",
        "_run_cloud_readiness", "cloud_topolog",
    ):
        assert banned not in src, banned

    assert "runtime_profile_evidence_validation" in src
    assert "required_checks" in src
    assert "REQUIRED_CHECK_STEP_KEY" in validator_src

    module_src = inspect.getsource(orch)
    assert "_synthesis_declares_services" not in module_src


def _incomplete_resigned(project: Path) -> dict:
    """Produce a real attested pass, drop a required check, re-sign, return the
    resigned payload dict."""
    _write_runtime_results(project, 0)
    data = read_attested_wave(project, 0)
    assert data["overall_passed"] is True
    rt = project / ".aah" / "build" / "runtime-results" / "wave-0-all.json"
    data["checks"].pop("build", None)
    _re_sign(project, rt, data)
    return read_attested_wave(project, 0)


def test_runtime_profile_rollout_states(tmp_path, monkeypatch):
    """AC5: the same incomplete (re-signed) artifact yields report_only → None
    + audit record, enforce → block (env var set still blocks), off → None; and
    an enforce end-to-end genuine pass → None.
    """
    proj_ro = _real_wave_project(tmp_path / "ro", runtime_profile_v1="report_only")
    assert _runtime_profile_rollout(proj_ro / ".aah") == "report_only"
    resigned_ro = _incomplete_resigned(proj_ro)
    audit_path = proj_ro / ".aah" / "audit" / "gate-decisions.jsonl"
    assert _runtime_profile_gate(proj_ro, proj_ro / ".aah", 0, resigned_ro) is None
    assert audit_path.exists()
    audit_txt = audit_path.read_text()
    assert '"gate": "runtime_profile"' in audit_txt
    assert '"decision": "report_only"' in audit_txt

    proj_en = _real_wave_project(tmp_path / "en", runtime_profile_v1=True)
    assert _runtime_profile_rollout(proj_en / ".aah") == "enforce"
    resigned_en = _incomplete_resigned(proj_en)
    monkeypatch.setenv("AAH_RUNTIME_PROFILE_V1", "off")
    monkeypatch.setenv("AAH_VERIFY_INTERNAL", "0")
    assert _runtime_profile_rollout(proj_en / ".aah") == "enforce"
    gate_en = _runtime_profile_gate(proj_en, proj_en / ".aah", 0, resigned_en)
    assert gate_en is not None
    assert gate_en["action"] == "user_confirm"
    assert gate_en["runtime_profile_state"] == "enforce"
    monkeypatch.delenv("AAH_RUNTIME_PROFILE_V1", raising=False)
    monkeypatch.delenv("AAH_VERIFY_INTERNAL", raising=False)

    proj_off = _real_wave_project(tmp_path / "off", runtime_profile_v1=False)
    assert _runtime_profile_rollout(proj_off / ".aah") == "off"
    resigned_off = _incomplete_resigned(proj_off)
    assert _runtime_profile_gate(proj_off, proj_off / ".aah", 0, resigned_off) is None

    proj_pass = _real_wave_project(tmp_path / "pass", runtime_profile_v1=True)
    _write_runtime_results(proj_pass, 0)
    genuine = read_attested_wave(proj_pass, 0)
    assert _runtime_profile_gate(proj_pass, proj_pass / ".aah", 0, genuine) is None
