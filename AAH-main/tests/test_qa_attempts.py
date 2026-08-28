"""Functional tests for authoritative, subject-bound QA attempt history.

NO MOCKS: Every test uses real Git repositories, real worktrees, real feature
contracts, real attestation secrets, and real CLI subprocess calls.

Tests verify:
- AC1: Each run writes an attested attempt and matching test evidence
- AC2: derive_qa_state is idempotent (repeated reads are byte-equal)
- AC3: Rerun evidence (attempt-NNN-tests.json) is subject-bound
- AC4: Latest-attempt state is readable by the orchestrator
- AC5: New SHA creates new attempt; prior attempts become historical
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from aah.core.common.git_utils import code_subject_identity
from tests.support.aah_project import AAHProjectBuilder


def _configure_feature(builder: AAHProjectBuilder, test_files: str) -> None:
    builder.feature(
        "F001",
        frontmatter={
            "spec_ref": "SPEC-001",
            "description": "QA attempt history for F001",
            "dependencies": [],
            "status": "pending",
            "test_config": {
                "command": f"{sys.executable} -m pytest {test_files} -s"
            },
            "acceptance_criteria": [{"id": "AC1", "description": "test runs"}],
            "test_cases": [
                {"id": "TC1", "covers": ["AC1"], "description": "marker validation"}
            ],
            "knowledge_used": {"knowledge_folder": None},
        },
    )


@pytest.fixture
def qa_project(tmp_path: Path) -> dict:
    """Create a real Git project with feature F001, worktree, and attestation secret."""
    builder = AAHProjectBuilder.create(tmp_path)
    project = builder.path
    test_rel = "qa_tests/test_F001.py"
    builder.file(".gitignore", ".aah/build/\n.claude/\n").manifest(
        project_name="qa-attempts-test",
    )
    _configure_feature(builder, test_rel)
    builder.file(
        test_rel,
        "import os\n"
        "from pathlib import Path\n\n"
        "def test_F001_TC1_marker():\n"
        "    marker = Path('marker.txt').read_text().strip()\n"
        "    assert marker.startswith('commit-')\n",
    ).file("marker.txt", "commit-base\n").commit(
        "test: seed QA attempts project",
        (".gitignore", ".aah", "qa_tests", "marker.txt"),
    )

    worktree = project / ".claude" / "worktrees" / "F001"
    builder.git("worktree", "add", str(worktree), "-b", "feature/F001", "develop")
    builder.secret()

    return {"builder": builder, "project": project, "worktree": worktree, "shas": {}}


def _make_commit(state: dict, marker: str) -> str:
    """Create a commit in the worktree and return its subject identity.

    Returns the .aah/.claude-excluding content identity (what the system
    stamps under freshness v2), so it matches the stored subject.commit_sha.
    """
    worktree = state["worktree"]
    (worktree / "marker.txt").write_text(f"commit-{marker}\n", encoding="utf-8")
    worktree_builder = AAHProjectBuilder(worktree)
    worktree_builder.git("add", "marker.txt")
    worktree_builder.git("commit", "-m", f"test: marker {marker}")
    sha = code_subject_identity(cwd=worktree)
    state["shas"][marker] = sha
    return sha


def _run_qa_report(
    state: dict,
    *,
    verdict: str = "rework_required",
    subject_sha: str | None = None,
    issues_json: str = "[]",
) -> subprocess.CompletedProcess:
    """Run write_qa_report CLI with given verdict and subject SHA."""
    project = state["project"]
    worktree = state["worktree"]
    subject_sha = subject_sha or code_subject_identity(cwd=worktree)

    args = [
        "run",
        "core.build.write_qa_report",
        "--feature-id",
        "F001",
        "--verdict",
        verdict,
        "--criteria-json",
        '[{"id":"AC1","verdict":"' + ("pass" if verdict == "pass" else "fail") + '","description":"test","evidence":"testing"}]',
        "--test-command",
        "pytest qa_tests/test_F001.py",
        "--tests-run",
        "1",
        "--tests-passed",
        "1" if verdict == "pass" else "0",
        "--project-path",
        str(project),
        "--subject-path",
        str(worktree),
        "--subject-branch",
        "feature/F001",
        "--subject-sha",
        subject_sha,
        "--actor",
        "qa",
    ]

    if issues_json != "[]":
        args.extend(["--issues-json", issues_json])

    return state["builder"].run_module("aah.cli", *args)




def test_AC2_derive_state_idempotent(qa_project):
    """AC2: derive_qa_state is idempotent — repeated reads are byte-equal."""
    sha1 = _make_commit(qa_project, "sha1")
    result = _run_qa_report(qa_project, verdict="rework_required", subject_sha=sha1)
    assert result.returncode == 0, result.stderr
    sha2 = _make_commit(qa_project, "sha2")
    result = _run_qa_report(qa_project, verdict="rework_required", subject_sha=sha2)
    assert result.returncode == 0, result.stderr
    sha3 = _make_commit(qa_project, "sha3")
    result = _run_qa_report(qa_project, verdict="rework_required", subject_sha=sha3)
    assert result.returncode == 0, result.stderr

    project = qa_project["project"]
    attempts_dir = project / ".aah" / "build" / "qa-results" / "F001"

    assert (attempts_dir / "attempt-001.json").exists()
    assert (attempts_dir / "attempt-002.json").exists()
    assert (attempts_dir / "attempt-003.json").exists()

    from aah.core.build.qa_evidence import allocate_attempt, derive_qa_state
    from aah.core.common.attestation import write_attested

    state1 = derive_qa_state(project, "F001")
    state2 = derive_qa_state(project, "F001")

    state1_json = json.dumps(state1, sort_keys=True)
    state2_json = json.dumps(state2, sort_keys=True)
    assert state1_json == state2_json, "derive_qa_state must be idempotent"

    assert state1["attempts_total"] == 3
    assert state1["rework_count"] == 3  # All rework_required
    assert state1["current_attempt"] == 3
    assert state1["current_subject_sha"] == sha3

    attempt_no, attempt_path = allocate_attempt(project, "F001")
    assert attempt_no == 4
    write_attested(
        {
            "schema_version": 1,
            "feature_id": "F001",
            "verdict": "fail",
            "subject": {"commit_sha": sha3},
        },
        attempt_path,
        project_path=project,
        command=["aah", "run", "core.build.write_qa_report"],
        exit_code=0,
        stdout="",
        stderr="",
        duration_ms=0,
    )
    legacy_state = derive_qa_state(project, "F001")
    assert legacy_state["rework_count"] == 4
    assert legacy_state["current_verdict"] == "rework_required"


def test_AC3_rerun_evidence_subject_bound(qa_project):
    """AC3: attempt-001-tests.json exists, verifies, and is subject-bound."""
    sha1 = _make_commit(qa_project, "sha1")

    result = _run_qa_report(qa_project, verdict="rework_required", subject_sha=sha1)
    assert result.returncode == 0, result.stderr

    project = qa_project["project"]
    attempts_dir = project / ".aah" / "build" / "qa-results" / "F001"

    tests_path = attempts_dir / "attempt-001-tests.json"
    assert tests_path.exists(), "attempt-001-tests.json must exist"

    tests_data = json.loads(tests_path.read_text(encoding="utf-8"))

    assert "attestation" in tests_data
    assert tests_data["attestation"]["signature"]

    assert tests_data["subject"]["commit_sha"] == sha1
    assert tests_data["subject"]["branch"] == "feature/F001"

    attempt_path = attempts_dir / "attempt-001.json"
    attempt_data = json.loads(attempt_path.read_text(encoding="utf-8"))
    assert tests_data["subject"]["commit_sha"] == attempt_data["subject"]["commit_sha"]




def _run_feature_tests(state: dict) -> subprocess.CompletedProcess:
    """Run feature tests to generate passing Tier 1 evidence."""
    project = state["project"]
    worktree = state["worktree"]
    sha = code_subject_identity(cwd=worktree)

    return state["builder"].run_module(
        "aah.cli",
        "run", "core.build.run_feature_tests",
        "--feature-id", "F001",
        "--project-path", str(project),
        "--subject-path", str(worktree),
        "--subject-branch", "feature/F001",
        "--subject-sha", sha,
        "--actor", "implementer",
        "--attempt-id", "attempt-001",
    )


def test_AC5_new_sha_new_attempt_prior_historical(qa_project):
    """AC5: Two rework_required on SHA A (001,002), one run on SHA B (003). Attempts on A are historical."""
    sha_a = _make_commit(qa_project, "shaA")

    _run_qa_report(qa_project, verdict="rework_required", subject_sha=sha_a)
    _run_qa_report(qa_project, verdict="rework_required", subject_sha=sha_a)

    project = qa_project["project"]
    attempts_dir = project / ".aah" / "build" / "qa-results" / "F001"

    attempt_001_path = attempts_dir / "attempt-001.json"
    attempt_001_before = attempt_001_path.read_bytes()
    hash_before = hashlib.sha256(attempt_001_before).hexdigest()

    sha_b = _make_commit(qa_project, "shaB")

    _run_qa_report(qa_project, verdict="rework_required", subject_sha=sha_b)

    attempt_001_after = attempt_001_path.read_bytes()
    hash_after = hashlib.sha256(attempt_001_after).hexdigest()
    assert hash_before == hash_after, "attempt-001.json must never be overwritten"

    from aah.core.build.qa_evidence import derive_qa_state

    state = derive_qa_state(project, "F001")
    assert state["current_subject_sha"] == sha_b
    assert state["current_attempt"] == 3
    assert state["attempts_total"] == 3
    assert state["rework_count"] == 3  # All rework_required

    history = state["history"]
    assert len(history) == 3

    assert history[0]["attempt"] == 1
    assert history[0]["subject_sha"] == sha_a
    assert history[0]["historical"] is True

    assert history[1]["attempt"] == 2
    assert history[1]["subject_sha"] == sha_a
    assert history[1]["historical"] is True

    assert history[2]["attempt"] == 3
    assert history[2]["subject_sha"] == sha_b
    assert history[2]["historical"] is False


def test_AC1_writer_accepts_three_rejects_others(qa_project):
    """AC1: CLI argparse accepts pass/rework_required/human_review_required; rejects fail and bogus."""
    _make_commit(qa_project, "sha1")
    project = qa_project["project"]

    result_rework = _run_qa_report(qa_project, verdict="rework_required")
    assert result_rework.returncode == 0, result_rework.stderr
    assert "invalid choice" not in result_rework.stderr

    result_human = _run_qa_report(qa_project, verdict="human_review_required")
    assert result_human.returncode == 0, result_human.stderr
    assert "invalid choice" not in result_human.stderr

    result_pass = _run_qa_report(qa_project, verdict="pass")
    assert "invalid choice: 'pass'" not in result_pass.stderr, "Argparse must accept --verdict pass"

    result_fail = _run_qa_report(qa_project, verdict="fail")
    assert "invalid choice: 'fail'" in result_fail.stderr, "CLI must reject --verdict fail"
    assert all(choice in result_fail.stderr for choice in (
        "pass", "rework_required", "human_review_required"
    ))

    result_bogus = _run_qa_report(qa_project, verdict="bogus")
    assert "invalid choice: 'bogus'" in result_bogus.stderr, "CLI must reject --verdict bogus"
    assert all(choice in result_bogus.stderr for choice in (
        "pass", "rework_required", "human_review_required"
    ))

    attempts_dir = project / ".aah" / "build" / "qa-results" / "F001"
    all_attempts = sorted(attempts_dir.glob("attempt-*.json"))
    non_test_attempts = [a for a in all_attempts if not a.name.endswith("-tests.json")]

    assert len(non_test_attempts) == 2, f"Expected 2 attempts, got {len(non_test_attempts)}: {[a.name for a in non_test_attempts]}"

    attempt1_data = json.loads(non_test_attempts[0].read_text(encoding="utf-8"))
    attempt2_data = json.loads(non_test_attempts[1].read_text(encoding="utf-8"))
    verdicts = {attempt1_data["verdict"], attempt2_data["verdict"]}
    assert verdicts == {"rework_required", "human_review_required"}


def test_AC2_rework_findings_shape_no_code(qa_project):
    """AC2: rework_required findings have AC/TC IDs, evidence, requested_behavior; NO fix field."""
    _make_commit(qa_project, "sha1")
    project = qa_project["project"]

    issues = json.dumps([
        {
            "issue_id": "ISS001",
            "affected_ac_ids": ["AC1"],
            "affected_tc_ids": ["TC1"],
            "severity": "major",
            "evidence": "Test missing assertion on output",
            "requested_behavior": "Output must match expected format"
        },
        {
            "issue_id": "ISS002",
            "affected_ac_ids": ["AC2", "AC3"],
            "affected_tc_ids": [],
            "severity": "critical",
            "evidence": "No test covers timeout scenario",
            "requested_behavior": "System must respond within 300ms under normal load"
        }
    ])

    result = _run_qa_report(qa_project, verdict="rework_required", issues_json=issues)
    assert result.returncode == 0, result.stderr

    attempts_dir = project / ".aah" / "build" / "qa-results" / "F001"
    attempt_path = attempts_dir / "attempt-001.json"
    attempt_data = json.loads(attempt_path.read_text(encoding="utf-8"))
    assert len(attempt_data["issues"]) == 2
    for issue in attempt_data["issues"]:
        assert "issue_id" in issue
        assert "affected_ac_ids" in issue
        assert "affected_tc_ids" in issue
        assert "severity" in issue
        assert "evidence" in issue
        assert "requested_behavior" in issue
        assert "fix" not in issue


def test_AC3_rework_gap_closed_by_rerun_traceability(qa_project):
    """AC3: QA → rework_required naming AC2 uncovered; implementer adds test; rerun flips to pass; NO qa-results authored test."""
    project = qa_project["project"]
    worktree = qa_project["worktree"]

    _make_commit(qa_project, "sha1")

    issues = json.dumps([{
        "issue_id": "GAP_AC2",
        "affected_ac_ids": ["AC2"],
        "affected_tc_ids": [],
        "severity": "critical",
        "evidence": "No test exercises AC2 behavior",
        "requested_behavior": "AC2: System must validate input format"
    }])
    result1 = _run_qa_report(qa_project, verdict="rework_required", issues_json=issues)
    assert result1.returncode == 0

    attempts_dir = project / ".aah" / "build" / "qa-results" / "F001"
    qa_results_tests = list(attempts_dir.glob("*.py"))
    assert len(qa_results_tests) == 0, "QA must not author tests (read-only)"

    ac2_test = worktree / "qa_tests" / "test_ac2.py"
    ac2_test.write_text(
        "def test_F001_AC2_input_validation():\n"
        "    from pathlib import Path\n"
        "    marker = Path('marker.txt').read_text().strip()\n"
        "    assert marker.startswith('commit-')\n",
        encoding="utf-8"
    )
    AAHProjectBuilder(worktree).git("add", "qa_tests/test_ac2.py")
    AAHProjectBuilder(worktree).git("commit", "-m", "feat: add AC2 test")

    test_rel = "qa_tests/test_F001.py qa_tests/test_ac2.py"
    _configure_feature(qa_project["builder"], test_rel)

    result_tests = _run_feature_tests(qa_project)
    assert result_tests.returncode == 0

    result2 = _run_qa_report(qa_project, verdict="pass")
    assert result2.returncode == 0

    qa_results_tests_after = list(attempts_dir.glob("*.py"))
    assert len(qa_results_tests_after) == 0, "QA must not author test files"

    assert ac2_test.exists()
    assert ac2_test.parent == worktree / "qa_tests"


def _write_profile_config(project: Path, feature_id: str, level: str, reasons=None) -> str:
    """Write a checkpoint-config.yaml with a stored profile; return its hash."""
    import yaml as _yaml

    from aah.core.build.verification_profiles import profile_hash

    profiles = {
        feature_id: {
            "level": level,
            "rule_version": "1",
            "reasons": reasons or ([] if level == "standard" else ["explicit_security_scope"]),
            "override": None,
        }
    }
    cfg = {"checkpoint_configuration": {"verification_profiles": profiles}}
    cfg_path = project / ".aah" / "plan" / "checkpoint-config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(_yaml.dump(cfg), encoding="utf-8")
    return profile_hash(profiles[feature_id])


def test_profile_hash_binds_qa_attempt(qa_project):
    """AC7: the official QA writer persists the verification-profile
    (level, rule_version, reasons, config hash, override ref) into each attempt
    and a checkpoint-config change makes that attempt's
    routing hash stale."""
    project = qa_project["project"]
    _make_commit(qa_project, "sha1")

    original_hash = _write_profile_config(project, "F001", "deep")

    result = _run_qa_report(qa_project, verdict="rework_required")
    assert result.returncode == 0, result.stderr

    attempt_path = project / ".aah" / "build" / "qa-results" / "F001" / "attempt-001.json"
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    vp = attempt["verification_profile"]
    assert vp["level"] == "deep"
    assert vp["rule_version"] == "1"
    assert "explicit_security_scope" in vp["reasons"]
    assert vp["checkpoint_config_hash"] == original_hash
    assert vp["no_signal"] is None

    changed_hash = _write_profile_config(
        project, "F001", "deep", reasons=["explicit_security_scope", "high_fanout"]
    )
    assert changed_hash != original_hash

    from aah.core.build.qa_evidence import (
        latest_attempt,
        profile_binding_for_feature,
    )

    stored = (latest_attempt(project, "F001") or {}).get("verification_profile", {})
    current = profile_binding_for_feature(project, "F001")
    assert stored["checkpoint_config_hash"] != current["checkpoint_config_hash"]
