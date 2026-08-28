"""Tests: absence of QA-approval synthesis + mandatory QA.

This file's original premise — a deterministic Tier-1 shortcut
(`_synthesize_qa_approval_from_tier1`) that wrote a passing QA report and
marked a feature passing without independent QA — has been DELETED. QA is
now mandatory: every traceability-passing feature is dispatched to the
independent aah-qa-evaluator agent.

These tests pin the inverted contract:
  - the synthesis symbol no longer exists (AC1);
  - a passing Tier-1 result never writes a QA attempt nor flips
    feature-list.passes (AC2, AC3);
  - compute_next_action returns run_qa for a sequential wave (AC4);
  - the fail-open shortcut is gone: a passing Tier-1 does NOT skip QA;
  - a real writer/reader round-trip proves the reader path without
    mocking Tier 1 (AC5);
  - a parallel wave with no provable subject checkout blocks as
    no_signal instead of pointing QA at the root checkout (AC6).

NO MOCKS: real temp git repos, real attested files with the LIVE command
prefixes, and the real write_qa_report CLI. The only monkeypatch is the
merge-to-integration gate bypass — the established fixture pattern in the
sibling test file (test_orchestrator_spec_validation.py), which swaps a
git-state helper for a trivial function; it does not fake any QA verdict.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import sys

from aah.core.common.attestation import write_attested
from aah.core.common.io_utils import read_json, write_json
from aah.core.common.progress import get_default_progress, save_progress
from aah.core.build.evidence import (
    build_evidence_v2_record,
    capture_subject,
    hash_feature_contract,
    hash_test_inputs,
)
import aah.core.build.orchestrator as orch
from aah.core.build.orchestrator import compute_next_action
from aah.core.build.qa_routing import (
    _feature_qa_final_approved,
    _features_needing_qa,
)
from tests.support.aah_project import AAHProjectBuilder


_SPEC_VALIDATION_CMD = ["aah", "run", "core.build.validate_implementation"]
_FEATURE_TEST_CMD = ["aah", "run", "core.build.run_feature_tests"]


def _test_rel(feature_id: str) -> str:
    return f"qa_tests/test_{feature_id}.py"


def _add_feature(builder: AAHProjectBuilder, feature_id: str) -> None:
    test_rel = _test_rel(feature_id)
    builder.feature(
        feature_id,
        frontmatter={
            "spec_ref": "SPEC-001",
            "description": f"qa synthesis source for {feature_id}",
            "dependencies": [],
            "status": "pending",
            "test_config": {"command": f"{sys.executable} -m pytest {test_rel} -s"},
            "acceptance_criteria": [{"id": "AC1", "description": "marker present"}],
            "test_cases": [
                {"id": "TC1", "covers": ["AC1"], "description": "marker validation"}
            ],
        },
    ).file(test_rel, f"def test_{feature_id}_TC1_marker():\n    assert True\n")


@pytest.fixture
def project(git_repo):
    """A single-feature (F001) project on a clean committed git tree.

    Feature evidence binds to the project-ROOT subject, so the orchestrator's
    subject-freshness reader accepts a seeded passing test result as fresh for
    this single-feature (sequential) wave."""
    tmp_path = git_repo
    builder = AAHProjectBuilder(tmp_path).dirs(
        "plan",
        "plan/features",
        "build/test-results",
        "build/quality-results",
        "build/runtime-results",
        "build/checkpoint-results",
        "build/validation-results",
    )
    aah_root = builder.aah
    builder.manifest(project_name="qa-mandatory-test")

    progress = get_default_progress()
    progress["current_phase"] = "build"
    progress["current_wave"] = 0
    save_progress(progress, aah_root / "claude-progress.json")

    write_json(
        {"features": [
            {"id": "F001", "spec_ref": "S1", "description": "A",
             "dependencies": [], "passes": False},
        ]},
        aah_root / "feature-list.json",
    )
    builder.waves([["F001"]])
    _add_feature(builder, "F001")
    builder.file(".gitignore", ".aah/build/\n.claude/\n").secret().commit(
        "test: seed qa synthesis project", (".gitignore", ".aah", "qa_tests")
    )

    return tmp_path


@pytest.fixture
def two_feature_project(project):
    """Extends `project` with a second feature F002 in the same wave.

    Two available features → get_wave_context computes strategy="parallel".
    Each feature gets a clean per-feature worktree, so a parallel wave
    resolves a real worktree subject; the root-bound seed is STALE for that
    subject → the QA gate fails closed with no_signal (no path in the reason)."""
    builder = AAHProjectBuilder(project)
    aah_root = builder.aah
    write_json(
        {"features": [
            {"id": "F001", "spec_ref": "S1", "description": "A",
             "dependencies": [], "passes": False},
            {"id": "F002", "spec_ref": "S1", "description": "B",
             "dependencies": [], "passes": False},
        ]},
        aah_root / "feature-list.json",
    )
    builder.waves([["F001", "F002"]])
    _add_feature(builder, "F002")
    builder.commit("test: add F002", (".aah", "qa_tests"))

    for fid in ("F001", "F002"):
        worktree = project / ".claude" / "worktrees" / fid
        builder.git("worktree", "add", str(worktree), "-b", f"feature/{fid}", "HEAD")

    return project


@pytest.fixture
def bypass_merge_gate(monkeypatch):
    """Let the bare test repository reach QA dispatch."""
    monkeypatch.setattr(orch, "_features_not_merged_to_integration",
                        lambda *a, **k: [])
    monkeypatch.setattr(orch, "ensure_integration_branch",
                        lambda *a, **k: {"branch": "integration/wave-0", "created": False})


def _seed_attested(project: Path, rel: str, payload: dict, command: list[str]) -> Path:
    out = project / rel
    write_attested(
        payload, out, project_path=project, command=command,
        exit_code=0, stdout="", stderr="", duration_ms=1,
    )
    return out


def _feature_test_payload(project: Path, feature_id: str, *, passed: bool = True) -> dict:
    """Build a subject-bound feature-test payload for the project-ROOT subject.

    Uses the real evidence functions so the subject/contract/test-input hashes
    match what the orchestrator's freshness reader recomputes — no mocks. The
    legacy top-level keys the readers still consume are merged over the v2
    envelope."""
    test_rel = _test_rel(feature_id)
    envelope = build_evidence_v2_record(
        feature_id=feature_id,
        producer="run_feature_tests",
        subject=capture_subject(project, project),
        contract_hash=hash_feature_contract(
            project / ".aah" / "plan" / "features" / f"{feature_id}.md"
        ),
        test_input_hash=hash_test_inputs([project / test_rel], project),
        test_paths=[test_rel],
        execution={},
        status="pass" if passed else "fail",
        artifacts={},
    )
    envelope.update({
        "passed": passed,
        "summary": {"total": 1, "passed": 1 if passed else 0, "failed": 0 if passed else 1},
        "failures": [] if passed else [{"name": "test_x"}],
    })
    return envelope


def _seed_passing_test(project: Path, feature_id: str) -> None:
    _seed_attested(
        project,
        f".aah/build/test-results/{feature_id}.json",
        _feature_test_payload(project, feature_id, passed=True),
        _FEATURE_TEST_CMD,
    )


def _seed_qa_pass(project: Path, feature_id: str) -> None:
    """Seed an authoritative QA-passing attempt for the current subject."""
    from aah.core.build.verify import subject_identity
    from tests._qa_helpers import seed_qa_attempt

    seed_qa_attempt(
        project, feature_id, 1, "pass", subject_identity(project)
    )


def _seed_tier1_pass(project: Path, feature_id: str) -> None:
    _seed_attested(
        project,
        f".aah/build/validation-results/{feature_id}-spec-validation.json",
        {
            "feature_id": feature_id,
            "passed": True,
            "criteria_results": [
                {"id": "AC-01", "description": "AC1", "covered": True,
                 "passed": True, "evidence": "Feature tests pass"},
            ],
            "gaps": [],
            "details": {
                "total_criteria": 1, "total_test_cases": 1,
                "criteria_covered": 1, "criteria_total": 1,
                "coverage_ratio": 1.0, "gap_count": 0,
            },
        },
        _SPEC_VALIDATION_CMD,
    )


def _seed_tier1_fail_with_gaps(project: Path, feature_id: str) -> None:
    _seed_attested(
        project,
        f".aah/build/validation-results/{feature_id}-spec-validation.json",
        {
            "feature_id": feature_id,
            "passed": False,
            "criteria_results": [
                {"id": "AC-01", "description": "AC1", "covered": False, "evidence": None},
                {"id": "AC-02", "description": "AC2", "covered": False, "evidence": None},
            ],
            "gaps": [
                {"criterion_id": "AC-01", "description": "AC1",
                 "reason": "No test coverage found"},
                {"criterion_id": "AC-02", "description": "AC2",
                 "reason": "No test coverage found"},
            ],
            "details": {
                "total_criteria": 2, "total_test_cases": 0,
                "criteria_covered": 0, "criteria_total": 2,
                "coverage_ratio": 0.0, "gap_count": 2,
                "failure_reason": "Too many acceptance criteria uncovered",
            },
        },
        _SPEC_VALIDATION_CMD,
    )


def test_symbol_removed():
    """_synthesize_qa_approval_from_tier1 no longer exists (AC1)."""
    assert not hasattr(orch, "_synthesize_qa_approval_from_tier1"), (
        "the deterministic Tier-1 QA-approval synthesis helper must be "
        "deleted — QA is mandatory (P0.1-3)."
    )


class TestTier1PassNoSynthesis:
    def test_tier1_pass_no_qa_file_written_by_needing_qa(self, project):
        """A passing Tier-1 result dispatches the feature to QA and writes no
        authoritative attempt; feature-list.passes stays False (AC2, AC3)."""
        _seed_passing_test(project, "F001")
        _seed_tier1_pass(project, "F001")
        aah_root = project / ".aah"

        result = _features_needing_qa(project, aah_root, ["F001"])
        assert result == ["F001"], (
            f"mandatory QA: a traceability-passing feature must be dispatched "
            f"even when Tier 1 passed; got {result}"
        )

        attempts_dir = aah_root / "build" / "qa-results" / "F001"
        assert not attempts_dir.exists(), (
            "a passing Tier-1 result must not synthesize a QA attempt."
        )

        fl = read_json(aah_root / "feature-list.json")
        assert fl["features"][0]["passes"] is False, (
            "a passing Tier-1 result must not mark the feature passing."
        )

    def test_tier1_fail_still_dispatches_qa(self, project):
        """A failing Tier-1 result also dispatches the feature to QA."""
        _seed_passing_test(project, "F001")
        _seed_tier1_fail_with_gaps(project, "F001")
        aah_root = project / ".aah"

        result = _features_needing_qa(project, aah_root, ["F001"])
        assert result == ["F001"]
        assert not (aah_root / "build" / "qa-results" / "F001").exists()

    def test_tier1_missing_still_dispatches_qa(self, project):
        """No Tier-1 result at all → feature still dispatched to QA."""
        _seed_passing_test(project, "F001")
        aah_root = project / ".aah"
        assert _features_needing_qa(project, aah_root, ["F001"]) == ["F001"]


class TestComputeNextActionRunQa:
    def test_tier1_pass_yields_run_qa_sequential(self, project, bypass_merge_gate):
        """Single-feature wave → sequential strategy. A passing Tier-1
        yields action=run_qa (never a synthesized approval) (AC4)."""
        _ = bypass_merge_gate
        _seed_passing_test(project, "F001")
        _seed_tier1_pass(project, "F001")

        action = compute_next_action(project)
        assert action["action"] == "run_qa", (
            f"expected run_qa (QA is mandatory), got {action['action']!r}"
        )
        assert "F001" in action["features"]
        assert "tier1_gaps" not in action
        assert not (
            project / ".aah" / "build" / "qa-results" / "F001"
        ).exists()

    def test_tier1_fail_still_yields_subject_bound_run_qa(self, project, bypass_merge_gate):
        """QA recomputes deterministic gaps against its dispatched subject."""
        _ = bypass_merge_gate
        _seed_passing_test(project, "F001")
        _seed_tier1_fail_with_gaps(project, "F001")

        action = compute_next_action(project)
        assert action["action"] == "run_qa"
        assert "F001" in action["features"]
        assert "tier1_gaps" not in action
        assert action["subject_map"]["F001"]["subject_sha"]


class TestRealWriterReaderRoundTrip:
    def test_fail_verdict_report_is_not_a_qa_approval(self, project):
        """A real subject-bound rework attempt never counts as approval."""
        aah_root = project / ".aah"
        subject = capture_subject(project, project)
        result = AAHProjectBuilder(project).run_module(
            "aah.core.build.write_qa_report",
            "--feature-id", "F001",
            "--verdict", "rework_required",
            "--criteria-json",
            '[{"id":"AC1","description":"AC1","verdict":"fail","evidence":"missing"}]',
            "--issues-json",
            '[{"issue_id":"ISSUE-1","affected_ac_ids":["AC1"],"affected_tc_ids":["TC1"],"severity":"major","evidence":"AC1 not implemented","requested_behavior":"AC1 must be implemented and covered"}]',
            "--project-path", str(project),
            "--subject-path", str(project),
            "--subject-branch", subject["branch"],
            "--subject-sha", subject["commit_sha"],
        )
        assert result.returncode == 0, (
            f"write_qa_report --verdict rework_required should exit 0; "
            f"stdout={result.stdout!r} stderr={result.stderr[-500:]!r}"
        )

        qa_path = aah_root / "build" / "qa-results" / "F001" / "attempt-001.json"
        assert qa_path.exists(), "the real CLI must write an authoritative attempt"
        assert not _feature_qa_final_approved(project, aah_root, "F001")
        _seed_passing_test(project, "F001")
        assert _features_needing_qa(project, aah_root, ["F001"]) == ["F001"]


class TestParallelWaveFailsClosed:
    def test_parallel_wave_needing_qa_blocks_no_signal(
        self, two_feature_project, bypass_merge_gate
    ):
        """Two available features → parallel strategy. Both have attested
        passing tests + Tier-1 passed but no QA reports. compute_next_action
        must fail closed with no_signal + recreate_evidence, and must NOT
        write any QA report nor point QA at the root checkout (AC6)."""
        _ = bypass_merge_gate
        project = two_feature_project
        for fid in ("F001", "F002"):
            _seed_passing_test(project, fid)
            _seed_tier1_pass(project, fid)

        from aah.core.build.update_impl_state import get_wave_context
        assert get_wave_context(project, 0).get("strategy") == "parallel"

        action = compute_next_action(project)
        assert action["action"] == "no_signal", (
            f"parallel wave with no provable QA subject must fail closed; "
            f"got {action['action']!r}"
        )
        assert action.get("recreate_evidence") is True
        assert set(action["features"]) == {"F001", "F002"}

        for fid in ("F001", "F002"):
            assert not (
                project / ".aah" / "build" / "test-results" / f"{fid}-qa.json"
            ).exists()

        assert "project_dir" not in action
        assert str(project) not in str(action.get("reason", ""))
        for key in ("worktree_map", "worktree", "path", "subject", "checkout"):
            assert key not in action, (
                f"no_signal action must not name a QA subject/target ({key})."
            )

    def test_last_straggler_of_parallel_wave_still_blocks(
        self, two_feature_project, bypass_merge_gate
    ):
        """HIGH #1 regression: the guard must key off the static
        total_features_in_wave. Here F001 is already fully complete
        (passes=True + attested tests + QA approval), so only F002 remains.
        Normal tier dispatch remains parallel and the guard must still fire
        no_signal because F002 was implemented in its own worktree —
        dispatching QA against root would validate the wrong tree."""
        _ = bypass_merge_gate
        project = two_feature_project
        aah_root = project / ".aah"

        write_json(
            {"features": [
                {"id": "F001", "spec_ref": "S1", "description": "A",
                 "dependencies": [], "passes": True},
                {"id": "F002", "spec_ref": "S1", "description": "B",
                 "dependencies": [], "passes": False},
            ]},
            aah_root / "feature-list.json",
        )
        _seed_passing_test(project, "F001")
        _seed_qa_pass(project, "F001")

        _seed_passing_test(project, "F002")
        _seed_tier1_pass(project, "F002")

        from aah.core.build.update_impl_state import get_wave_context
        wctx = get_wave_context(project, 0)
        assert wctx.get("strategy") == "parallel"
        assert wctx.get("total_features_in_wave") == 2, (
            "total_features_in_wave must stay static at 2 (the whole wave)"
        )

        action = compute_next_action(project)
        assert action["action"] == "no_signal", (
            f"the last straggler of a parallel wave must STILL block "
            f"(root-checkout bypass); got {action['action']!r}"
        )
        assert action.get("recreate_evidence") is True
        assert "F002" in action["features"]
        assert not (aah_root / "build" / "qa-results" / "F002").exists()
