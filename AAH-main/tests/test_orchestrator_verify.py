"""Orchestrator readers reject forged or unattested verification artifacts.

The critical regression case prevents forged ``fix_category=design_issue``
runtime evidence from hijacking the rework path.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.aah_project import AAHProjectBuilder
from tests._qa_helpers import seed_qa_attempt

from aah.core.common.attestation import (
    write_attested,
)
from aah.core.build.evidence import (
    build_evidence_v2_record,
    capture_subject,
    hash_feature_contract,
    hash_test_inputs,
)
from aah.core.common.git_utils import code_subject_identity
from aah.core.common.io_utils import read_json, write_json, write_yaml
from aah.core.common.manifest import get_default_manifest, save_manifest
from aah.core.common.progress import get_default_progress, save_progress
from aah.core.build.orchestrator import (
    _project_standards_gate,
    compute_next_action,
    is_last_wave,
)
from aah.core.build.qa_routing import (
    _feature_qa_final_approved,
    _features_needing_qa,
)
from aah.core.build.verify import read_regression_evidence
from aah.core.common.git_utils import current_branch, rev_parse, run_git


def _seed_orchestrator_project(
    project_path: Path,
    *,
    name: str,
    features: list[dict],
    waves: list[list[str]],
    description_prefix: str = "feature",
) -> AAHProjectBuilder:
    builder = AAHProjectBuilder(project_path).dirs(
        "plan/features",
        "build/test-results",
        "build/runtime-results",
        "build/quality-results",
        "build/checkpoint-results",
    ).secret().waves(waves)
    manifest = get_default_manifest(name)
    manifest["current_phase"] = "build"
    save_manifest(manifest, builder.aah / "manifest.yaml")
    progress = get_default_progress()
    progress.update(current_phase="build", current_wave=0)
    save_progress(progress, builder.aah / "claude-progress.json")
    write_json({"features": features}, builder.aah / "feature-list.json")
    write_yaml(
        {
            "checkpoint_configuration": {
                "verification_profiles": {
                    feature["id"]: {
                        "level": "standard",
                        "rule_version": "test",
                        "reasons": ["orchestrator fixture"],
                        "required_checks": {},
                    }
                    for feature in features
                }
            }
        },
        builder.aah / "plan" / "checkpoint-config.yaml",
    )
    for feature in features:
        fid = feature["id"]
        write_yaml(
            {
                "id": fid,
                "description": f"{description_prefix} {fid}",
                "dependencies": feature.get("dependencies", []),
                "acceptance_criteria": ["AC1"],
                "test_cases": [{"id": "TC1", "covers": ["AC1"]}],
            },
            builder.aah / "plan" / "features" / f"{fid}.yaml",
        )
    return builder


@pytest.fixture
def project(git_repo):
    """A minimal project with the .aah skeleton and an attestation secret.

    Uses the conftest's `git_repo` fixture (a tmp dir initialized as a
    real git repo) because compute_next_action calls into branch_exists
    which requires a working git tree.
    """
    run_git(["branch", "integration/wave-0"], cwd=git_repo)
    return _seed_orchestrator_project(
        git_repo,
        name="orch-test",
        features=[{
            "id": "F001", "spec_ref": "S1", "description": "A",
            "dependencies": [], "passes": False,
        }],
        waves=[["F001"]],
        description_prefix="test",
    ).path


def _seed_legitimate(
    project: Path,
    rel_path: str,
    payload: dict,
    command: list[str],
) -> Path:
    """Write `payload` at `rel_path` with a valid attestation block under the project's secret."""
    out = project / rel_path
    write_attested(
        payload,
        out,
        project_path=project,
        command=command,
        exit_code=0,
        stdout="",
        stderr="",
        duration_ms=1,
    )
    return out


def _strip_attestation(path: Path) -> None:
    """Mutate a file on disk: remove its attestation block (simulates pre-Phase-2 file)."""
    data = read_json(path)
    data.pop("attestation", None)
    write_json(data, path)


def _tamper_signature(path: Path) -> None:
    """Mutate a file on disk: keep the attestation but invalidate the signature."""
    data = read_json(path)
    data["attestation"]["signature"] = "0" * 64
    write_json(data, path)


REGRESSION_PAYLOAD = {
    "passed": True,
    "exit_code": 0,
    "command": "uv run pytest tests/",
    "summary": {"total": 5, "passed": 5, "failed": 0},
    "failures": [],
    "subject": {
        "branch": "integration/wave-0",
        "commit_sha": "a" * 40,
    },
}
REGRESSION_CMD = ["aah", "run", "core.build.run_regression_suite"]


def _seed_qa_pass(project: Path) -> Path:
    subject_sha = code_subject_identity(cwd=project)
    assert subject_sha is not None
    return seed_qa_attempt(project, "F001", 1, "pass", subject_sha)


STANDARDS_PASS = {
    "scope": "project",
    "wave": 0,
    "overall_passed": True,
    "verdict": "PASS",
    "status": "pass",
    "linting": {"passed": True, "status": "pass"},
    "static_analysis": {"passed": True, "status": "pass"},
    "coverage": {"passed": True, "status": "not_applicable"},
    "type_check": {"passed": True, "status": "not_applicable"},
}
STANDARDS_CMD = ["aah", "run", "core.build.quality_checks"]


def _seed_project_standards(
    project: Path, payload: dict | None = None, *, wave: int = 0
) -> Path:
    """Seed attested project-scoped standards evidence bound to the wave branch."""
    seeded = dict(STANDARDS_PASS if payload is None else payload)
    seeded["wave"] = wave
    seeded["subject"] = {
        "branch": f"integration/wave-{wave}",
        "commit_sha": code_subject_identity(
            cwd=project, ref=f"integration/wave-{wave}"
        ),
    }
    return _seed_legitimate(
        project,
        f".aah/build/quality-results/wave-{wave}-project-standards.json",
        seeded,
        STANDARDS_CMD,
    )


class TestProjectStandardsGate:
    """The standards gate is project-scoped and fires ONCE, in the last wave."""

    def test_passing_evidence_emits_nothing(self, project):
        _seed_project_standards(project)
        assert _project_standards_gate(project, project / ".aah", 0) is None

    def test_missing_evidence_requests_the_project_run(self, project):
        action = _project_standards_gate(project, project / ".aah", 0)
        assert action["action"] == "run_project_standards"
        assert action["signal_reason"] == "missing_or_unverified"
        # Whole-codebase run: no feature scoping anywhere in the payload.
        assert "features" not in action
        assert "--feature-id" not in action["command"]

    def test_blocking_verdict_routes_to_fix_standards_not_blocked(self, project):
        payload = dict(STANDARDS_PASS)
        payload.update(
            overall_passed=False, verdict="BLOCK", status="fail",
            linting={"passed": False, "status": "fail"},
            findings={
                "linting": "s.py:1:1: UP035 `typing.List` is deprecated\n",
                "static_analysis": "",
            },
        )
        _seed_project_standards(project, payload)

        action = _project_standards_gate(project, project / ".aah", 0)
        assert action["action"] == "fix_standards", action
        # The four fields aah-fix's direct-repair fast path needs.
        assert action["route"] == "aah-fix"
        assert action["repair_mode"] == "current_wave_repair"
        assert action["source_artifact"].endswith("wave-0-project-standards.json")
        assert action["source_integration_sha"]
        # Verbatim tool output, not a parsed violations array.
        assert "UP035" in action["failures"]["linting"]

    def test_stale_subject_requests_a_re_run(self, project):
        payload = dict(STANDARDS_PASS)
        _seed_project_standards(project, payload)
        path = (
            project / ".aah" / "build" / "quality-results"
            / "wave-0-project-standards.json"
        )
        data = read_json(path)
        data["subject"]["commit_sha"] = "b" * 64
        _seed_legitimate(
            project,
            ".aah/build/quality-results/wave-0-project-standards.json",
            {k: v for k, v in data.items() if k != "attestation"},
            STANDARDS_CMD,
        )
        action = _project_standards_gate(project, project / ".aah", 0)
        assert action["action"] == "run_project_standards"
        assert action["signal_reason"] == "stale_subject"

    def test_unattested_evidence_requests_a_re_run(self, project):
        path = _seed_project_standards(project)
        _strip_attestation(path)
        action = _project_standards_gate(project, project / ".aah", 0)
        assert action["action"] == "run_project_standards"

    def test_tampered_signature_requests_a_re_run(self, project):
        path = _seed_project_standards(project)
        _tamper_signature(path)
        action = _project_standards_gate(project, project / ".aah", 0)
        assert action["action"] == "run_project_standards"

    def test_earlier_wave_is_a_no_op_not_a_failure(self, project):
        """The trap this gate must avoid: demanding, on waves 0..n-1, evidence
        that by design is only produced in the last wave."""
        write_json(
            {"waves": [["F001"], ["F002"]], "total_waves": 2},
            project / ".aah" / "plan" / "waves.json",
        )
        assert not is_last_wave(project / ".aah", 0)
        # No evidence seeded at all, and still no action.
        assert _project_standards_gate(project, project / ".aah", 0) is None
        assert is_last_wave(project / ".aah", 1)

    def test_is_last_wave_reads_waves_json_not_an_inference(self, project):
        write_json(
            {"waves": [["F001"], ["F002"], ["F003"]], "total_waves": 3},
            project / ".aah" / "plan" / "waves.json",
        )
        assert [is_last_wave(project / ".aah", w) for w in (0, 1, 2)] == [
            False, False, True,
        ]


FEATURE_TEST_PASS = {
    "feature_id": "F001",
    "passed": True,
    "exit_code": 0,
    "command": "uv run pytest tests/ -k F001",
    "summary": {"total": 1, "passed": 1, "failed": 0},
    "failures": [],
}
FEATURE_TEST_CMD = ["aah", "run", "core.build.run_feature_tests"]

_ARTIFACTS = {
    "regression": (
        ".aah/build/test-results/regression-latest.json",
        REGRESSION_PAYLOAD,
        REGRESSION_CMD,
    ),
    # Project-scoped, wave-keyed — NOT <fid>-standards.json. The per-feature key
    # is what made a deferred run impossible (its worktree subject is deleted at
    # promote), which is why the gate moved to project scope.
    "standards": (
        ".aah/build/quality-results/wave-0-project-standards.json",
        STANDARDS_PASS,
        STANDARDS_CMD,
    ),
    "feature": (".aah/build/test-results/F001.json", FEATURE_TEST_PASS, FEATURE_TEST_CMD),
}


def _seed_artifact(
    project: Path,
    kind: str,
    payload: dict | None = None,
    *,
    command: list[str] | None = None,
) -> Path:
    path, default_payload, default_command = _ARTIFACTS[kind]
    seeded_payload = dict(default_payload) if payload is None else payload
    if kind == "feature" and payload is None:
        feature_path = project / ".aah" / "plan" / "features" / "F001.yaml"
        test_path = project / "README.md"
        seeded_payload.update(
            build_evidence_v2_record(
                feature_id="F001",
                producer="run_feature_tests",
                subject=capture_subject(project, project),
                contract_hash=hash_feature_contract(feature_path),
                test_input_hash=hash_test_inputs([test_path], project),
                test_paths=["README.md"],
                execution={"argv": ["pytest"], "cwd": ".", "exit_code": 0},
                status="pass",
                artifacts={},
            )
        )
    if kind in ("regression", "standards") and payload is None:
        seeded_payload["subject"] = {
            "branch": "integration/wave-0",
            "commit_sha": code_subject_identity(cwd=project, ref="integration/wave-0"),
        }
    return _seed_legitimate(
        project,
        path,
        seeded_payload,
        default_command if command is None else command,
    )


def _seed_runtime(project: Path, payload: dict) -> Path:
    from aah.core.build.verification_identity import runtime_criteria_identity

    seeded = dict(payload)
    seeded.setdefault("subject", {
        "branch": "integration/wave-0",
        "commit_sha": code_subject_identity(
            cwd=project, ref="integration/wave-0"
        ),
    })
    seeded.setdefault(
        "runtime_criteria_sha256", runtime_criteria_identity(project, 0)
    )
    return _seed_legitimate(
        project,
        ".aah/build/runtime-results/wave-0-all.json",
        seeded,
        ["aah", "run", "core.build.write_runtime_results"],
    )


@pytest.fixture
def real_v2_feature_result(git_repo):
    """Produce an attested evidence-v2 F001 result through the real CLI."""
    builder = AAHProjectBuilder(git_repo)
    project = builder.path
    builder.git("config", "user.name", "Test")
    builder.git("config", "user.email", "test@example.com")
    builder.file(".gitignore", ".aah/build/\n").file(
        "subject_tests/test_F001.py",
        "def test_F001_TC1_real_v2_result():\n    assert 2 + 2 == 4\n",
    )
    command = f"{sys.executable} -m pytest subject_tests/test_F001.py -s"
    builder.manifest().feature(
        "F001",
        frontmatter={
            "spec_ref": "SPEC-001",
            "description": "real v2 orchestrator compatibility",
            "dependencies": [],
            "test_config": {"command": command},
            "acceptance_criteria": [{"id": "AC1", "description": "real pass"}],
            "test_cases": [{"id": "TC1", "covers": ["AC1"]}],
        },
        body="",
    ).commit("test: seed real v2 result", (
        ".gitignore", ".aah/manifest.yaml", ".aah/plan", "subject_tests",
    )).secret()
    branch = current_branch(cwd=project)
    # Production passes the freshness identity as --subject-sha, not raw HEAD.
    sha = code_subject_identity(cwd=project) or rev_parse("HEAD", cwd=project)
    completed = builder.run_module(
        "aah.cli", "run", "core.build.run_feature_tests",
            "--feature-id",
            "F001",
            "--project-path",
            str(project),
            "--subject-path",
            str(project),
            "--subject-branch",
            branch,
            "--subject-sha",
            sha,
            "--actor",
            "implementer",
            "--attempt-id",
            "attempt-001",
    )
    assert completed.returncode == 0, completed.stderr
    return project


class TestFeaturesNeedingQa:
    def test_legitimate_pass_with_no_qa_yet_enqueues(self, project):
        _seed_artifact(project, "feature")
        assert _features_needing_qa(project, project / ".aah", ["F001"]) == ["F001"]

    def test_no_attestation_does_not_enqueue(self, project):
        path = _seed_artifact(project, "feature")
        _strip_attestation(path)
        assert _features_needing_qa(project, project / ".aah", ["F001"]) == []

    def test_signature_mismatch_does_not_enqueue(self, project):
        path = _seed_artifact(project, "feature")
        _tamper_signature(path)
        assert _features_needing_qa(project, project / ".aah", ["F001"]) == []

    def test_real_evidence_v2_result_remains_compatible(self, real_v2_feature_result):
        project = real_v2_feature_result
        payload = read_json(project / ".aah/build/test-results/F001.json")
        assert payload["schema_version"] == 2
        assert payload["status"] == "pass"
        assert payload["subject"]["commit_sha"] == code_subject_identity(cwd=project)
        assert payload["subject"]["head_sha"] == rev_parse("HEAD", cwd=project)
        assert _features_needing_qa(project, project / ".aah", ["F001"]) == ["F001"]


class TestReworkEngineHijack:
    """Forged design-issue evidence must trigger a fresh checkpoint."""

    @pytest.fixture(autouse=True)
    def _bypass_git_merge_gate(self, monkeypatch):
        """Isolate the runtime-results reader from merge and branch gates."""
        monkeypatch.setenv("AAH_VERIFY_INTERNAL", "0")
        from aah.core.build import orchestrator as orch
        monkeypatch.setattr(
            orch,
            "_features_not_merged_to_integration",
            lambda *a, **k: [],
        )
        monkeypatch.setattr(
            orch,
            "ensure_integration_branch",
            lambda *a, **k: {"branch": "integration/wave-0", "created": False},
        )
        monkeypatch.setattr(orch, "current_branch", lambda *a, **k: "integration/wave-0")

    def _seed_completed_wave(self, project):
        """Reach the runtime-results read with every upstream gate passing."""
        aah_root = project / ".aah"

        write_json(
            {
                "features": [
                    {"id": "F001", "spec_ref": "S1", "description": "A",
                     "dependencies": [], "passes": True},
                ]
            },
            aah_root / "feature-list.json",
        )

        _seed_artifact(project, "feature")

        _seed_qa_pass(project)

        # No spec-validation artifact is seeded: its producer, its verifier
        # contract, and its write_qa_report gate are all gone (issue #117). No
        # system-checkpoint consolidation wrapper is seeded either — the signed
        # runtime result IS the system-checkpoint evidence now.

        _seed_artifact(project, "standards")

        _seed_artifact(project, "regression")


    def test_forged_runtime_results_with_design_issue_does_not_trigger_raise_feedback(
        self, project
    ):
        """The non-negotiable forged-evidence regression test."""
        self._seed_completed_wave(project)

        forged = {
            "wave": 0,
            "overall_passed": False,
            "checks": {
                "module_validation": {"passed": False, "details": {"message": "forged"}},
            },
            "fix_category": "design_issue",  # the hijack vector
        }
        write_json(
            forged,
            project / ".aah" / "build" / "runtime-results" / "wave-0-all.json",
        )

        action = compute_next_action(project)
        assert action["action"] == "run_system_checkpoint", (
            f"Forged runtime-results with fix_category=design_issue must "
            f"trigger run_system_checkpoint, not {action['action']}. "
            "This is the rework-engine-hijack regression."
        )
        assert action["action"] != "raise_feedback"
        assert action["action"] != "user_confirm"

    def test_legitimate_design_issue_does_trigger_raise_feedback(self, project):
        """Attested design-issue evidence still escalates to rework."""
        self._seed_completed_wave(project)

        legit = {
            "wave": 0,
            "overall_passed": False,
            "runtime_mode": "docker",
            "port": 8000,
            "checks": {
                "module_validation": {"passed": False, "details": {"message": "legitimate failure"}},
            },
            "summary": {"total_checks": 1, "passed": 0, "failed": 1},
            "fix_category": "design_issue",
        }
        _seed_runtime(project, legit)

        action = compute_next_action(project)
        assert action["action"] == "raise_feedback", (
            f"Legitimately attested runtime-results with fix_category=design_issue "
            f"should escalate to rework, got {action['action']}"
        )

    def test_signature_tampered_runtime_results_re_runs_checkpoint(self, project):
        """A tampered signature triggers a fresh checkpoint."""
        self._seed_completed_wave(project)

        legit = {
            "wave": 0,
            "overall_passed": False,
            "runtime_mode": "docker",
            "port": 8000,
            "checks": {"module_validation": {"passed": False, "details": {}}},
            "summary": {"total_checks": 1, "passed": 0, "failed": 1},
            "fix_category": "design_issue",
        }
        path = _seed_runtime(project, legit)
        _tamper_signature(path)

        action = compute_next_action(project)
        assert action["action"] == "run_system_checkpoint"
        assert action["action"] != "raise_feedback"

    def test_runtime_results_deleted_re_runs_checkpoint(self, project):
        """Deleting runtime results re-triggers checkpoint instead of merge."""
        self._seed_completed_wave(project)

        runtime_path = project / ".aah" / "build" / "runtime-results" / "wave-0-all.json"
        legit = {
            "wave": 0,
            "overall_passed": True,
            "runtime_mode": "docker",
            "port": 8000,
            "checks": {},
            "summary": {"total_checks": 0, "passed": 0, "failed": 0},
        }
        _seed_runtime(project, legit)
        runtime_path.unlink()
        assert not runtime_path.exists()

        action = compute_next_action(project)
        assert action["action"] == "run_system_checkpoint", (
            f"Deleting wave-N-all.json must re-trigger checkpoint, "
            f"got {action['action']!r}. If this fires as 'merge', the "
            f"runtime-results-deleted fall-through has regressed."
        )
        assert "missing or unverifiable" in action.get("reason", ""), (
            "reason string should signal the file's missing-or-unverified status"
        )



class TestPrePhase2Upgrade:
    """Wave-completion scope.

    ``wave_completed`` now reduces to the ``passes`` flag: the per-feature
    attested-evidence term and the QA-approval term were both removed when the
    per-feature QA step was disabled (5.1.1 §3), because the steps that produce
    what they required are no longer invoked. Keeping either term would mean no
    feature ever completes.

    Global ``completed`` was never in scope for that change and stays loose for
    cross-wave DAG resolution — the second half of the original scope decision,
    still pinned below.
    """

    @pytest.fixture(autouse=True)
    def _bypass_git_merge_gate(self, monkeypatch):
        from aah.core.build import orchestrator as orch
        monkeypatch.setattr(orch, "_features_not_merged_to_integration",
                            lambda *a, **k: [])
        monkeypatch.setattr(orch, "ensure_integration_branch",
                            lambda *a, **k: {"branch": "integration/wave-0", "created": False})

    def test_un_attested_passing_feature_completes_wave(self, project):
        """passes=True with an UN-ATTESTED per-feature test result now COMPLETES
        the wave.

        This is the inversion of the old pre-phase-2 behaviour, asserted
        explicitly so it is not later "fixed" back into a re-dispatch. The
        attested-evidence term is gone from ``wave_completed`` because its
        producer gate is no longer invoked; re-dispatching here would rebuild a
        feature that is already done, forever.
        """
        aah_root = project / ".aah"
        write_json({"features": [
            {"id": "F001", "spec_ref": "S1", "description": "A",
             "dependencies": [], "passes": True},
        ]}, aah_root / "feature-list.json")
        write_json(
            {"feature_id": "F001", "passed": True,
             "summary": {"total": 1, "passed": 1, "failed": 0}, "failures": []},
            aah_root / "build" / "test-results" / "F001.json",
        )

        action = compute_next_action(project)
        assert not action["action"].startswith("dispatch_"), (
            "un-attested passing feature must still be wave-complete "
            f"(evidence gate is disabled), got {action['action']!r}"
        )

    def test_passing_feature_completes_wave_without_qa_report(self, project):
        """No QA report at all and no attested test evidence → still complete.

        The old version of this test seeded both an attested feature-test result
        and a passing QA report to reach this outcome. Neither is required now:
        the QA evaluator is never dispatched, so a wave that waited for a QA
        report would wait forever.
        """
        aah_root = project / ".aah"
        write_json({"features": [
            {"id": "F001", "spec_ref": "S1", "description": "A",
             "dependencies": [], "passes": True},
        ]}, aah_root / "feature-list.json")

        action = compute_next_action(project)
        assert not action["action"].startswith("dispatch_"), (
            f"passing feature must be wave-complete with no QA evidence, "
            f"got {action['action']!r}"
        )
        assert not (aah_root / "build" / "qa-results").exists(), (
            "no qa-results/ may be written by the disabled QA step"
        )

    def test_global_completed_remains_loose_for_historical_waves(self, project):
        """A historical wave's feature (current_wave > 0) must stay in
        global `completed` even if its old per-feature test result is
        un-attested. Otherwise the DAG frontier breaks for current-wave
        features that depend on historical ones."""
        aah_root = project / ".aah"
        progress = read_json(aah_root / "claude-progress.json")
        progress["current_wave"] = 1
        write_json(progress, aah_root / "claude-progress.json")
        (aah_root / "audit").mkdir(exist_ok=True)
        write_json(
            {"cleanup_events": [{"wave": 0, "ts": "2026-06-01T00:00:00Z"}]},
            aah_root / "audit" / "branch-cleanup-log.json",
        )
        write_json(
            {"wave": 0, "indexed_at": "2026-06-01T00:00:00Z"},
            aah_root / "build" / "wave-0-codemap-indexed.json",
        )
        summary_dir = aah_root / "build" / "wave-summaries"
        summary_dir.mkdir(parents=True, exist_ok=True)
        (summary_dir / "wave-0-summary.md").write_text("# Wave 0 summary\n")

        write_json({"features": [
            {"id": "F001", "spec_ref": "S1", "description": "A",
             "dependencies": [], "passes": True},
            {"id": "F002", "spec_ref": "S1", "description": "B",
             "dependencies": ["F001"], "passes": False},
        ]}, aah_root / "feature-list.json")
        write_json({"waves": [["F001"], ["F002"]], "total_waves": 2},
                   aah_root / "plan" / "waves.json")
        write_yaml(
            {"id": "F002", "description": "depends on F001",
             "dependencies": ["F001"],
             "acceptance_criteria": ["AC1"],
             "test_cases": [{"id": "TC1", "covers": ["AC1"]}]},
            aah_root / "plan" / "features" / "F002.yaml",
        )

        from aah.core.common.dag import build_dag_from_features, dag_to_json
        feature_specs = [
            {"id": "F001", "dependencies": []},
            {"id": "F002", "dependencies": ["F001"]},
        ]
        G = build_dag_from_features(feature_specs)
        write_json(dag_to_json(G), aah_root / "plan" / "dag.json")

        action = compute_next_action(project)
        assert action["action"].startswith("dispatch_"), (
            f"historical wave feature must remain in global completed; "
            f"current-wave feature should dispatch, got {action['action']!r}"
        )


@pytest.fixture
def dispatch_project(git_repo):
    """A project on a real git repo with a develop branch and a 2-feature
    wave-0 (both passes=False), so compute_next_action reaches dispatch.

    Real git worktrees are created by setup_wave_worktrees during dispatch
    (branched off develop) — NO MOCKS. The attestation secret is seeded so
    downstream writers don't crash; no attested files are needed to reach
    the fresh-dispatch branch (all features passes=False).
    """
    tmp_path = git_repo
    run_git(["branch", "develop"], cwd=tmp_path)

    return _seed_orchestrator_project(
        tmp_path,
        name="orch-dispatch-test",
        features=[
            {"id": "F001", "spec_ref": "S1", "description": "A",
             "dependencies": [], "passes": False},
            {"id": "F002", "spec_ref": "S1", "description": "B",
             "dependencies": [], "passes": False},
        ],
        waves=[["F001", "F002"]],
    ).path


class TestDispatchSubjectDescriptor:
    """The DISPATCH payload retains a full worktree descriptor
    and carries a per-feature subject_map with subject_path / _branch / _sha.
    """

    def test_parallel_dispatch_worktree_map_is_rich(self, dispatch_project):
        action = compute_next_action(dispatch_project)
        assert action["action"] == "dispatch_parallel"
        wt = action["worktree_map"]
        assert set(wt.keys()) == {"F001", "F002"}
        for fid, desc in wt.items():
            assert set(desc.keys()) == {"path", "branch", "created", "sha"}
            assert desc["branch"] == "feature/" + fid
            assert len(desc["sha"]) == 40
            assert all(c in "0123456789abcdef" for c in desc["sha"])

    def test_parallel_dispatch_carries_subject_map(self, dispatch_project):
        action = compute_next_action(dispatch_project)
        assert action["action"] == "dispatch_parallel"
        subj = action["subject_map"]
        wt = action["worktree_map"]
        assert set(subj.keys()) == {"F001", "F002"}
        for fid in ("F001", "F002"):
            entry = subj[fid]
            assert entry["subject_branch"] == "feature/" + fid
            # subject_sha is the .aah/.claude-excluding freshness identity, not
            # the raw worktree HEAD SHA carried in worktree_map.
            assert entry["subject_sha"] == code_subject_identity(cwd=Path(wt[fid]["path"]))
            assert entry["subject_path"].endswith(f".claude/worktrees/{fid}")

    def test_worktree_map_path_backward_compat_accessor(self, dispatch_project):
        action = compute_next_action(dispatch_project)
        assert action["worktree_map"]["F001"]["path"].endswith(
            ".claude/worktrees/F001"
        )

    def test_single_feature_normal_dispatch_uses_worktree_subject(self, dispatch_project):
        aah_root = dispatch_project / ".aah"
        write_json(
            {
                "features": [
                    {"id": "F001", "spec_ref": "S1", "description": "A",
                     "dependencies": [], "passes": False},
                ]
            },
            aah_root / "feature-list.json",
        )
        write_json({"waves": [["F001"]], "total_waves": 1},
                   aah_root / "plan" / "waves.json")

        action = compute_next_action(dispatch_project)
        assert action["action"] == "dispatch_parallel"
        assert action["strategy"] == "parallel"
        assert set(action["worktree_map"]) == {"F001"}
        worktree = action["worktree_map"]["F001"]
        subj = action["subject_map"]["F001"]
        assert subj["subject_path"] == worktree["path"]
        assert subj["subject_branch"] == worktree["branch"]
        assert subj["subject_sha"] == code_subject_identity(cwd=Path(worktree["path"]))

    def test_forward_rework_dispatch_carries_worktree_subject(self, dispatch_project):
        aah_root = dispatch_project / ".aah"
        for extra in ["audit", "build/wave-summaries"]:
            (aah_root / extra).mkdir(parents=True, exist_ok=True)

        progress = read_json(aah_root / "claude-progress.json")
        progress["current_wave"] = 1
        write_json(progress, aah_root / "claude-progress.json")
        write_json(
            {
                "features": [
                    {"id": "F001", "spec_ref": "S1", "description": "A",
                     "dependencies": [], "passes": False},
                    {"id": "F002", "spec_ref": "S1", "description": "B",
                     "dependencies": [], "passes": False,
                     "supersedes": "F001"},
                ]
            },
            aah_root / "feature-list.json",
        )
        write_json({"waves": [["F001"], ["F002"]], "total_waves": 2},
                   aah_root / "plan" / "waves.json")

        write_json(
            {"active_reworks": [{
                "feedback_id": "FB1", "status": "in_progress",
                "root_cause_phase": "build", "category": "regression",
                "affected_features": ["F001"], "rework_features": ["F001"],
                "revert_done": True, "phase_reentry_done": True,
            }]},
            aah_root / "build" / "rework-state.json",
        )
        write_json({"cleanup_events": [{"wave": 0, "ts": "2026-06-01T00:00:00Z"}]},
                   aah_root / "audit" / "branch-cleanup-log.json")
        write_json({"wave": 0, "indexed_at": "2026-06-01T00:00:00Z"},
                   aah_root / "build" / "wave-0-codemap-indexed.json")
        (aah_root / "build" / "wave-summaries" / "wave-0-summary.md").write_text("# Wave 0\n")

        action = compute_next_action(dispatch_project)
        assert action["action"] == "dispatch_parallel"
        assert "rework" not in action
        worktree = action["worktree_map"]["F002"]
        subj = action["subject_map"]["F002"]
        assert subj["subject_path"] == worktree["path"]
        assert subj["subject_branch"] == worktree["branch"]
        assert subj["subject_sha"] == code_subject_identity(cwd=Path(worktree["path"]))


def test_AC5_no_signal_when_worktree_missing_wrong_branch_advanced_or_dirty(git_repo):
    """AC5: compute_next_action returns no_signal+recreate_evidence for worktree issues."""
    from aah.core.build.orchestrator import compute_next_action
    from aah.core.common.git_utils import run_git

    project = git_repo
    _seed_orchestrator_project(
        project,
        name="parallel-test",
        features=[
            {"id": "F001", "spec_ref": "S1", "description": "A",
             "dependencies": [], "passes": True},
            {"id": "F002", "spec_ref": "S2", "description": "B",
             "dependencies": [], "passes": True},
        ],
        waves=[["F001", "F002"]],
        description_prefix="test",
    )
    run_git(["checkout", "-b", "develop"], cwd=project)
    (project / "README.md").write_text("project\n")
    run_git(["add", "README.md"], cwd=project)
    run_git(["commit", "-m", "init"], cwd=project)

    worktree_base = project / ".claude" / "worktrees"
    worktree_base.mkdir(parents=True)

    for fid in ("F001", "F002"):
        worktree = worktree_base / fid
        run_git(["worktree", "add", str(worktree), "-b", f"feature/{fid}", "develop"], cwd=project)
        (worktree / f"{fid}.txt").write_text(f"{fid}\n")
        run_git(["add", f"{fid}.txt"], cwd=worktree)
        run_git(["commit", "-m", f"feat({fid}): add marker"], cwd=worktree)

        sha = rev_parse("HEAD", cwd=worktree)
        _seed_legitimate(
            project,
            f".aah/build/test-results/{fid}.json",
            {
                "feature_id": fid,
                "passed": True,
                "summary": {"total": 1, "passed": 1},
                "test_cases": [],
                "subject": {
                    "rel_path": f".claude/worktrees/{fid}",
                    "branch": f"feature/{fid}",
                    "commit_sha": sha,
                    "clean": True,
                },
                "inputs": {
                    "contract_hash": "abc123",
                    "test_input_hash": "def456",
                    "test_paths": ["tests/test.py"],
                },
            },
            ["aah", "run", "core.build.run_feature_tests"],
        )

        # No spec-validation artifact: the verifier no longer declares that
        # per-feature evidence contract (issue #117).

    run_git(["worktree", "remove", str(worktree_base / "F001")], cwd=project)
    action = compute_next_action(project)
    assert action["action"] == "no_signal"
    assert action.get("recreate_evidence") is True
    assert "F001" in action.get("features", [])
    assert "not a registered git worktree" in action.get("reason", "")

    run_git(["worktree", "add", str(worktree_base / "F001"), "-b", "feature/F001-v2", "develop"], cwd=project)
    (worktree_base / "F001" / "F001.txt").write_text("F001\n")
    run_git(["add", "F001.txt"], cwd=worktree_base / "F001")
    run_git(["commit", "-m", "feat(F001): recreate"], cwd=worktree_base / "F001")

    action = compute_next_action(project)
    assert action["action"] == "no_signal"
    assert action.get("recreate_evidence") is True
    assert "F001" in action.get("features", [])
    assert "branch" in action.get("reason", "").lower()

    run_git(["worktree", "remove", str(worktree_base / "F001")], cwd=project)
    run_git(["branch", "-D", "feature/F001-v2"], cwd=project)
    run_git(["branch", "-D", "feature/F001"], cwd=project)  # Delete old branch too
    run_git(["worktree", "add", str(worktree_base / "F001"), "-b", "feature/F001", "develop"], cwd=project)
    (worktree_base / "F001" / "F001.txt").write_text("F001\n")
    run_git(["add", "F001.txt"], cwd=worktree_base / "F001")
    run_git(["commit", "-m", "feat(F001): fix branch"], cwd=worktree_base / "F001")

    (worktree_base / "F001" / "new.txt").write_text("new\n")
    run_git(["add", "new.txt"], cwd=worktree_base / "F001")
    run_git(["commit", "-m", "feat(F001): advance HEAD"], cwd=worktree_base / "F001")

    action = compute_next_action(project)
    assert action["action"] == "no_signal"
    assert action.get("recreate_evidence") is True
    assert "F001" in action.get("features", [])
    assert "stale" in action.get("reason", "").lower()

    run_git(["reset", "--hard", "HEAD~1"], cwd=worktree_base / "F001")

    (worktree_base / "F001" / "dirty.txt").write_text("dirty\n")

    action = compute_next_action(project)
    assert action["action"] == "no_signal"
    assert action.get("recreate_evidence") is True
    assert "F001" in action.get("features", [])
    assert "dirty" in action.get("reason", "").lower() or "uncommitted" in action.get("reason", "").lower()


def test_runtime_gate_sha_bound(tmp_path):
    """AC4: with runtime_profile_v1 enforced + a fresh confirmed profile, a
    genuine verify pass satisfies the gate (control) — but advancing the
    integration/wave-0 HEAD by one commit makes the recorded integration_sha
    stale, so the gate blocks with an integration_sha_drift problem.

    Real project, real git integration branch, real confirmed profile, real
    verify CLI. No orchestrator monkeypatch.
    """
    from tests._runtime_helpers import (
        confirm_profile_cli,
        make_project,
        read_attested_wave,
    )
    from aah.core.build.orchestrator import _runtime_profile_gate

    git_env = {
        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": "/usr/bin:/bin",
    }

    def git(project, *args):
        return subprocess.run(
            ["git", *args], cwd=project, capture_output=True, text=True, env=git_env,
        )

    project = make_project(
        tmp_path, topology="full-local", transport="localhost",
        runtime_profile_v1=True, with_cloud_readiness=False,
    )
    aah_path = project / ".aah"
    git(project, "init", "-b", "main")
    git(project, "add", "-A")
    git(project, "commit", "-m", "init")
    git(project, "branch", "integration/wave-0")
    git(project, "checkout", "integration/wave-0")
    confirm_profile_cli(project)

    # Runtime evidence now comes from its real producer — verify.py is read-only
    # and no longer writes anything.
    from aah.core.build.verification_identity import (
        resolve_runtime_verification_profile,
        subject_identity,
    )

    profile, _ = resolve_runtime_verification_profile(project)
    head = subject_identity(project)
    write_attested(
        {
            "wave": 0, "overall_passed": True, "runtime_mode": "docker", "port": 8000,
            "checks": {
                key: {"check": key, "passed": True, "skipped": False}
                for key in ("build", "server_start", "smoke_tests", "cloud_readiness")
            },
            "summary": {"total_checks": 4, "passed": 4, "failed": 0},
            "subject": {"branch": "integration/wave-0", "commit_sha": head},
            "runtime_evidence": {
                "integration_sha": head,
                "profile_bindings": {"profile_hash": profile["profile_hash"]},
                "required_checks": list(profile["required_checks"]),
            },
        },
        aah_path / "build" / "runtime-results" / "wave-0-all.json",
        project_path=project,
        command=["aah", "run", "core.build.write_runtime_results", "--wave", "0"],
        exit_code=0, stdout="", stderr="", duration_ms=1,
    )
    data = read_attested_wave(project, 0)
    assert data["overall_passed"] is True

    assert _runtime_profile_gate(project, aah_path, 0, data) is None

    # Advance the subject with a REAL source change (an empty commit does not
    # move the .aah/.claude-excluding content identity).
    (project / "src.py").write_text("print('advance')\n", encoding="utf-8")
    git(project, "add", "-A")
    git(project, "commit", "-m", "advance integration subject")
    assert subject_identity(project) != head

    gate = _runtime_profile_gate(project, aah_path, 0, data)
    assert gate is not None
    assert gate["action"] == "user_confirm"
    assert gate["runtime_profile_state"] == "enforce"
    assert "integration_sha_drift" in gate["reason"]
    assert "integration_sha_drift" in gate["failures"]
