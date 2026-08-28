"""Human-in-the-loop QA review workflow tests.

NO MOCKS. Reuses subject_project fixture pattern from test_qa_subject_freshness.py:
real git repos, real worktrees, real attestation secret, real CLI subprocess calls.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from aah.core.common.git_utils import code_subject_identity
from tests.support.aah_project import AAHProjectBuilder


@pytest.fixture
def v2_project(tmp_path: Path) -> dict:
    """Real git project using the authoritative QA workflow."""
    builder = AAHProjectBuilder.create(tmp_path)
    project = builder.path
    builder.manifest(
        project_name="v2-test",
    ).waves([{"features": ["F001"]}])
    builder.file(
        ".aah/plan/checkpoint-config.yaml",
        yaml.safe_dump(
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
            }
        ),
    )

    test_rel = "qa_tests/test_F001.py"
    builder.file(
        test_rel,
        "from pathlib import Path\n\n"
        "def test_F001_TC1_marker():\n"
        "    marker = Path('marker.txt').read_text().strip()\n"
        "    assert marker.startswith('commit-')\n",
    ).file("marker.txt", "commit-base\n")

    command = f"{sys.executable} -m pytest {project / test_rel} -s"
    builder.feature(
        "F001",
        frontmatter={
            "description": "Test feature",
            "spec_ref": "SPEC-001",
            "dependencies": [],
            "status": "pending",
            "test_config": {"command": command},
            "acceptance_criteria": [{"id": "AC1", "description": "Must work"}],
            "test_cases": [
                {"id": "TC1", "covers": ["AC1"], "description": "marker validation"}
            ],
            "knowledge_used": {"knowledge_folder": None},
        },
    )

    builder.file(
        ".aah/plan/feature-list.json",
        '{"features": [{"id": "F001", "name": "F001", "passes": false}]}\n',
    )

    builder.file(
        ".aah/claude-progress.json",
        '{"current_wave": 0, "current_phase": "build"}\n',
    ).secret()

    builder.file(".gitignore", ".aah/\n.claude/\n")
    builder.commit("init", (".gitignore", "qa_tests", "marker.txt"))

    return {"builder": builder, "project": project}


def _run_feature_tests(project: Path, feature_id: str, passed: bool = True) -> tuple[int, str, str]:
    """Run feature tests CLI against the project-root subject (single-feature wave).

    The feature YAML's test_config.command controls pass/fail (the v2_project
    fixture uses ``echo pass`` → exit 0 → passing). The ``passed`` param is
    retained for call-site readability but the real command determines outcome.
    Subject binding: for a single-feature wave the subject IS the
    project root, so we stamp --subject-path/-branch/-sha with the root's live
    branch + HEAD so the evidence is bound to the current SHA.
    """
    builder = AAHProjectBuilder(project)
    branch = builder.git("rev-parse", "--abbrev-ref", "HEAD")
    sha = code_subject_identity(cwd=project)
    result = builder.run_module(
        "aah.cli",
        "run",
        "core.build.run_feature_tests",
        "--feature-id", feature_id,
        "--project-path", str(project),
        "--subject-path", str(project),
        "--subject-branch", branch,
        "--subject-sha", sha,
    )
    return (result.returncode, result.stdout, result.stderr)


def _write_qa_report(
    project: Path, feature_id: str, verdict: str, issues: list[dict] | None = None
) -> tuple[int, str, str]:
    """Run write_qa_report CLI and return (rc, out, err).

    Uses the real arg names: --criteria-json / --issues-json (not --summary/
    --issues), and stamps the subject descriptor (project root for a
    single-feature wave) so the attempt is bound to the current HEAD SHA — which
    is what derive_qa_state keys on. verdict ∈ {pass, rework_required,
    human_review_required}.

    CRITICAL: For verdict==pass, invokes via DIRECT MODULE (not aah.cli run)
    so Tier-1 re-gate failures surface as rc!=0. The aah.cli wrapper masks
    sys.exit(2) as rc 0 with no attempt written.
    """
    if issues is None:
        issues = (
            [{"issue_id": "ISSUE-1", "affected_ac_ids": ["AC1"],
              "affected_tc_ids": ["TC1"], "severity": "major",
              "evidence": "needs work", "requested_behavior": "AC1 must hold"}]
            if verdict != "pass" else []
        )
    criteria = [{"id": "AC1", "description": "Must work",
                 "verdict": "pass" if verdict == "pass" else "fail",
                 "evidence": "evaluated"}]
    builder = AAHProjectBuilder(project)
    branch = builder.git("rev-parse", "--abbrev-ref", "HEAD")
    sha = code_subject_identity(cwd=project)

    module = "aah.core.build.write_qa_report" if verdict == "pass" else "aah.cli"
    prefix = () if verdict == "pass" else ("run", "core.build.write_qa_report")
    result = builder.run_module(
        module,
        *prefix,
        "--feature-id", feature_id,
        "--project-path", str(project),
        "--verdict", verdict,
        "--criteria-json", json.dumps(criteria),
        "--issues-json", json.dumps(issues),
        "--subject-path", str(project),
        "--subject-branch", branch,
        "--subject-sha", sha,
    )
    return (result.returncode, result.stdout, result.stderr)


def _write_passing_spec_validation(project: Path, feature_id: str) -> None:
    """Produce current subject-bound spec validation through the real CLI."""
    builder = AAHProjectBuilder(project)
    branch = builder.git("rev-parse", "--abbrev-ref", "HEAD")
    sha = code_subject_identity(cwd=project)
    result = builder.run_module(
        "aah.cli",
        "run", "core.build.validate_implementation", "validate",
        "--feature-id", feature_id,
        "--project-path", str(project),
        "--subject-path", str(project),
        "--subject-branch", branch,
        "--subject-sha", sha,
    )
    assert result.returncode == 0, result.stderr


def _write_quality_check(project: Path, feature_id: str, verdict: str) -> None:
    """Write quality check artifact manually (quality_checks CLI may not exist yet)."""
    from aah.core.common.attestation import write_attested

    quality_path = project / ".aah" / "build" / "quality-results" / f"{feature_id}-standards.json"
    quality_path.parent.mkdir(parents=True, exist_ok=True)

    write_attested(
        {"feature_id": feature_id, "verdict": verdict, "timestamp": "2026-07-15T00:00:00Z"},
        quality_path,
        project_path=project,
        command=["aah", "run", "core.build.quality_checks", "run", "--feature-id", feature_id],
        exit_code=0,
        stdout="",
        stderr="",
        duration_ms=0,
        artifact_name=f"{feature_id} quality check",
    )


def _write_passing_test_evidence(project: Path, feature_id: str) -> None:
    """Produce attested passing feature-test evidence via the REAL runner.

    The earlier hand-written approach wrote FXXX-tests.json with a `status`
    key, but the orchestrator's _features_needing_qa reads FXXX.json and checks
    the boolean `passed` key — so the hand-written artifact was invisible and the
    feature fell through to dispatch. Delegate to the real run_feature_tests CLI
    (feature YAML command is `echo pass` → exit 0 → passed=True), which writes a
    correctly-attested FXXX.json bound to the current HEAD SHA (single-feature
    wave → subject is the project root). NO MOCKS.
    """
    rc, out, err = _run_feature_tests(project, feature_id, passed=True)
    assert rc == 0, f"run_feature_tests failed rc={rc}: {err}"


def _decision(
    project: Path,
    action: str,
    rationale: str = "",
) -> subprocess.CompletedProcess:
    """Invoke the simple prompt-response recorder."""
    args = [
        action,
        "--feature-id", "F001",
        "--project-path", str(project),
    ]
    if action == "feature-qa-request-rework":
        args.extend(["--rationale", rationale])
    return AAHProjectBuilder(project).run_module(
        "aah.core.build.validate_checkpoint",
        *args,
    )


def test_ac1_first_qa_run_creates_attempt_001(v2_project):
    project = v2_project["project"]
    from aah.core.build.orchestrator import compute_next_action
    from aah.core.build.qa_evidence import derive_qa_state

    fl_path = project / ".aah" / "plan" / "feature-list.json"
    fl = json.loads(fl_path.read_text())
    fl["features"][0]["passes"] = True
    fl_path.write_text(json.dumps(fl), encoding="utf-8")

    _write_passing_test_evidence(project, "F001")

    _write_passing_spec_validation(project, "F001")

    _write_quality_check(project, "F001", "PASS")

    action = compute_next_action(project)

    assert action["action"] == "run_qa", f"Expected run_qa, got {action['action']}: {action.get('reason', '')}"
    assert "F001" in action["features"]
    assert "subject_map" in action
    assert "qa_state" in action
    assert action["qa_state"]["F001"]["attempts_total"] == 0

    rc, stdout, stderr = _write_qa_report(project, "F001", "pass")
    assert rc == 0, f"write_qa_report failed: {stderr}"

    attempt_path = project / ".aah" / "build" / "qa-results" / "F001" / "attempt-001.json"
    assert attempt_path.exists()

    state = derive_qa_state(project, "F001")
    assert state["attempts_total"] == 1
    assert state["current_attempt"] == 1
    assert state["current_verdict"] == "pass"


def test_ac2_polls_create_zero_new_attempts(v2_project):
    project = v2_project["project"]
    from aah.core.build.orchestrator import compute_next_action

    fl_path = project / ".aah" / "plan" / "feature-list.json"
    fl = json.loads(fl_path.read_text())
    fl["features"][0]["passes"] = True
    fl_path.write_text(json.dumps(fl), encoding="utf-8")

    _write_passing_test_evidence(project, "F001")
    _write_passing_spec_validation(project, "F001")
    _write_qa_report(project, "F001", "pass")
    _write_quality_check(project, "F001", "PASS")

    for i in range(5):
        action = compute_next_action(project)
        assert action["action"] != "run_qa", f"Iteration {i}: unexpected run_qa"

    attempts_dir = project / ".aah" / "build" / "qa-results" / "F001"
    attempt_files = [
        p for p in attempts_dir.glob("attempt-*.json")
        if not p.name.endswith("-tests.json")
    ]
    assert len(attempt_files) == 1


def test_ac3_rework_cap_escalates_to_human(v2_project):
    project = v2_project["project"]
    from aah.core.build.orchestrator import compute_next_action

    fl_path = project / ".aah" / "plan" / "feature-list.json"
    fl = json.loads(fl_path.read_text())
    fl["features"][0]["passes"] = True
    fl_path.write_text(json.dumps(fl), encoding="utf-8")

    for attempt_num in range(1, 4):  # 3 rework attempts
        (project / f"fix{attempt_num}.txt").write_text(f"Fix {attempt_num}")
        AAHProjectBuilder(project).git("add", ".")
        AAHProjectBuilder(project).git("commit", "-m", f"fix {attempt_num}")

        _write_passing_test_evidence(project, "F001")

        _write_passing_spec_validation(project, "F001")

        _write_quality_check(project, "F001", "PASS")

        _write_qa_report(project, "F001", "rework_required")

    action = compute_next_action(project)

    assert action["action"] == "human_review_required", f"Expected human_review_required, got {action['action']}"
    assert action["feature_id"] == "F001"
    assert action["trigger"] == "rework_cap"
    assert action["rework_count"] == 3
    assert action["attempts_total"] == 3


def test_ac4_new_sha_pass_marks_priors_historical(v2_project):
    project = v2_project["project"]

    from aah.core.build.qa_evidence import derive_qa_state
    from aah.core.build.orchestrator import compute_next_action

    _write_passing_test_evidence(project, "F001")
    _write_passing_spec_validation(project, "F001")
    _write_qa_report(project, "F001", "rework_required")

    (project / "fix.txt").write_text("Fixed")
    AAHProjectBuilder(project).git("add", ".")
    AAHProjectBuilder(project).git("commit", "-m", "fix")

    sha2 = code_subject_identity(cwd=project)

    _write_passing_test_evidence(project, "F001")
    _write_passing_spec_validation(project, "F001")
    _write_qa_report(project, "F001", "pass")

    state = derive_qa_state(project, "F001")

    assert state["attempts_total"] == 2
    assert state["current_verdict"] == "pass"
    assert state["current_subject_sha"] == sha2
    assert state["history"][0]["historical"] is True  # old SHA
    assert state["history"][1]["historical"] is False  # current SHA

    _write_quality_check(project, "F001", "PASS")
    fl_path = project / ".aah" / "plan" / "feature-list.json"
    fl = json.loads(fl_path.read_text())
    fl["features"][0]["passes"] = True
    fl_path.write_text(json.dumps(fl), encoding="utf-8")

    action = compute_next_action(project)
    assert action["action"] != "run_qa"  # Feature approved, moves on


def test_ac5_approve_records_prompt_response(v2_project):
    project = v2_project["project"]

    sha1 = code_subject_identity(cwd=project)

    _write_passing_test_evidence(project, "F001")
    _write_passing_spec_validation(project, "F001")
    _write_qa_report(project, "F001", "rework_required")
    _write_quality_check(project, "F001", "PASS")

    result = _decision(project, "feature-qa-approve")
    assert result.returncode == 0, result.stderr
    decisions_dir = project / ".aah" / "build" / "qa-results" / "F001"
    first = json.loads((decisions_dir / "human-001.json").read_text())
    assert first["decision"] == "approve"
    assert first["attempt"] == 1
    assert "subject" not in first
    assert "attestation" not in first
    from aah.core.build.verification_evidence import (
        feature_qa_approval_validation,
    )
    _attempt, problem = feature_qa_approval_validation(
        project, "F001", sha1
    )
    assert problem is None

    legacy = dict(first)
    legacy.pop("attempt")
    legacy.pop("recorded_by")
    legacy["subject"] = {"commit_sha": sha1}
    decision_path = decisions_dir / "human-001.json"
    decision_path.write_text(json.dumps(legacy), encoding="utf-8")
    _attempt, problem = feature_qa_approval_validation(project, "F001", sha1)
    assert problem is None

    legacy["subject"]["commit_sha"] = "0" * 64
    decision_path.write_text(json.dumps(legacy), encoding="utf-8")
    _attempt, problem = feature_qa_approval_validation(project, "F001", sha1)
    assert problem == "qa_verdict_rework_required"

    (project / "fix.txt").write_text("Fixed issues")
    AAHProjectBuilder(project).git("add", ".")
    AAHProjectBuilder(project).git("commit", "-m", "fix issues")
    _write_passing_test_evidence(project, "F001")
    _write_passing_spec_validation(project, "F001")
    _write_quality_check(project, "F001", "PASS")
    _write_qa_report(project, "F001", "pass")

    result = _decision(project, "feature-qa-approve")
    assert result.returncode == 0, f"Should accept, got rc={result.returncode}: {result.stderr}"
    assert (decisions_dir / "human-002.json").exists()


def test_ac6_request_rework_returns_to_implementer(v2_project):
    project = v2_project["project"]

    from aah.core.build.orchestrator import compute_next_action

    fl_path = project / ".aah" / "plan" / "feature-list.json"
    fl = json.loads(fl_path.read_text())
    fl["features"][0]["passes"] = True
    fl_path.write_text(json.dumps(fl), encoding="utf-8")

    for attempt_num in range(1, 4):
        (project / f"fix{attempt_num}.txt").write_text(f"Fix {attempt_num}")
        AAHProjectBuilder(project).git("add", ".")
        AAHProjectBuilder(project).git("commit", "-m", f"fix {attempt_num}")
        _write_passing_test_evidence(project, "F001")
        _write_passing_spec_validation(project, "F001")
        _write_quality_check(project, "F001", "PASS")  # Required gate before QA
        _write_qa_report(project, "F001", "rework_required")

    action = compute_next_action(project)
    assert action["action"] == "human_review_required"

    result = _decision(
        project,
        "feature-qa-request-rework",
        "Needs more work on X",
    )
    assert result.returncode == 0, f"request-rework failed: {result.stderr}"

    action = compute_next_action(project)
    assert action["action"] == "rework_qa_feedback"
    assert action["feature_id"] == "F001"
    assert action["subject_map"]["F001"]["subject_path"] == str(project)
    assert action["human_rationale"] == "Needs more work on X"

    status = AAHProjectBuilder(project).git("status", "--porcelain")
    assert status == ""


def test_ac7_append_only_decisions_no_overwrite(v2_project):
    project = v2_project["project"]

    _write_passing_test_evidence(project, "F001")
    _write_passing_spec_validation(project, "F001")
    _write_quality_check(project, "F001", "PASS")
    _write_qa_report(project, "F001", "pass")

    result = _decision(project, "feature-qa-approve")
    assert result.returncode == 0, f"First approve failed: {result.stderr}"

    decisions_dir = project / ".aah" / "build" / "qa-results" / "F001"
    assert (decisions_dir / "human-001.json").exists()

    result = _decision(
        project,
        "feature-qa-request-rework",
        "Actually needs more testing",
    )
    assert result.returncode == 0, f"Request rework failed: {result.stderr}"

    assert (decisions_dir / "human-002.json").exists()

    for decision_file in [decisions_dir / "human-001.json", decisions_dir / "human-002.json"]:
        data = json.loads(decision_file.read_text())
        assert data["recorded_by"] == "user"
        assert data["attempt"] == 1
        assert "subject" not in data
        assert "attestation" not in data

    result = _decision(project, "feature-qa-approve")
    assert result.returncode == 0, result.stderr
    assert (decisions_dir / "human-003.json").exists()
