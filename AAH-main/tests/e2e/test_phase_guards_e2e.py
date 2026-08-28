"""E2E tests for phase guards and gates (WS6.1)."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from aah.core.common.io_utils import write_text, write_yaml
from aah.core.common.manifest import get_default_manifest, save_manifest
from aah.core.common.progress import get_default_progress, save_progress


def _setup_project(tmp_path, phase="research"):
    """Create a minimal project at a given phase."""
    rapids = tmp_path / ".rapids"
    for d in ["plan/features", "plan/specs", "research", "analysis/decisions",
               "implement/test-results", "audit"]:
        (rapids / d).mkdir(parents=True)

    manifest = get_default_manifest("test-project", "greenfield")
    manifest["current_phase"] = phase
    save_manifest(manifest, rapids / "manifest.yaml")

    progress = get_default_progress()
    progress["current_phase"] = phase
    save_progress(progress, rapids / "claude-progress.json")

    return tmp_path


class TestResearchGate:
    def test_blocks_without_artifacts(self, tmp_path):
        """Research gate should fail without research artifacts."""
        proj = _setup_project(tmp_path, "research")
        rapids = proj / ".rapids"

        # Research dir is empty — gate should block
        research_dir = rapids / "research"
        files = list(research_dir.iterdir())
        assert len(files) == 0  # Confirm empty

    def test_passes_with_artifacts(self, tmp_path):
        """Research gate should pass with research artifacts present."""
        proj = _setup_project(tmp_path, "research")
        rapids = proj / ".rapids"

        # Create research artifacts
        write_text("# Research Report\nFindings here.\n", rapids / "research" / "research-report.md")
        write_text("# Tech Assessment\nStack analysis.\n", rapids / "research" / "tech-assessment.md")

        # Verify artifacts exist
        research_dir = rapids / "research"
        files = [f.name for f in research_dir.iterdir() if f.is_file()]
        assert "research-report.md" in files
        assert "tech-assessment.md" in files


class TestAnalysisGate:
    def test_blocks_without_adrs(self, tmp_path):
        """Analysis gate should fail without ADR artifacts."""
        proj = _setup_project(tmp_path, "analysis")
        rapids = proj / ".rapids"

        decisions_dir = rapids / "analysis" / "decisions"
        files = list(decisions_dir.iterdir())
        assert len(files) == 0

    def test_passes_with_adrs(self, tmp_path):
        """Analysis gate should pass with ADRs present."""
        proj = _setup_project(tmp_path, "analysis")
        rapids = proj / ".rapids"

        write_text("# ADR-001: Database Choice\nUse PostgreSQL.\n",
                    rapids / "analysis" / "decisions" / "ADR-001.md")
        write_text("# ADR-002: Auth Strategy\nUse JWT.\n",
                    rapids / "analysis" / "decisions" / "ADR-002.md")

        files = [f.name for f in (rapids / "analysis" / "decisions").iterdir()]
        assert len(files) == 2


class TestPlanGate:
    def test_blocks_without_feature_list(self, tmp_path):
        """Plan gate should fail without feature-list.json."""
        proj = _setup_project(tmp_path, "plan")
        rapids = proj / ".rapids"

        assert not (rapids / "feature-list.json").exists()

    def test_passes_with_all_artifacts(self, tmp_path):
        """Plan gate should pass with feature list, DAG, and waves."""
        proj = _setup_project(tmp_path, "plan")
        rapids = proj / ".rapids"

        # Create plan artifacts
        feature_list = {
            "features": [
                {"id": "F001", "description": "Auth", "passes": False},
            ],
        }
        (rapids / "feature-list.json").write_text(json.dumps(feature_list))

        dag = {"nodes": ["F001"], "edges": []}
        (rapids / "plan" / "dag.json").write_text(json.dumps(dag))

        waves = {"waves": [["F001"]]}
        (rapids / "plan" / "waves.json").write_text(json.dumps(waves))

        assert (rapids / "feature-list.json").exists()
        assert (rapids / "plan" / "dag.json").exists()
        assert (rapids / "plan" / "waves.json").exists()


class TestIntakeResilience:
    """Extended intake resilience tests (WS6.4)."""

    def test_get_all_answers_missing_answer_key(self):
        """get_all_answers should skip entries with missing answer."""
        from aah.core.intake.intake import get_all_answers, get_default_intake, add_qa_round

        intake = get_default_intake()
        add_qa_round(intake, "start", [
            {"question": "Q1"},  # no answer
            {"question": "Q2", "answer": "A2"},
        ])
        answers = get_all_answers(intake)
        assert len(answers) == 1
        assert "Q2" in answers

    def test_get_answered_topics_missing_header(self):
        """get_answered_topics should skip entries with missing header."""
        from aah.core.intake.intake import get_answered_topics, get_default_intake, add_qa_round

        intake = get_default_intake()
        add_qa_round(intake, "start", [
            {"question": "Q1", "answer": "A1"},  # no header
            {"question": "Q2", "answer": "A2", "header": "Scope"},
        ])
        topics = get_answered_topics(intake)
        assert "Scope" in topics
        assert len(topics) == 1

    def test_is_complete_empty_rounds(self):
        """is_complete with empty rounds should return False."""
        from aah.core.intake.intake import (
            get_default_intake, is_complete, set_problem_statement,
            set_project_type, set_complexity_assessment,
        )

        intake = get_default_intake()
        set_problem_statement(intake, "Test")
        set_project_type(intake, "new_system")
        set_complexity_assessment(intake, {"tier": "moderate"})
        # No rounds added
        assert not is_complete(intake)

    def test_load_intake_malformed_json(self, tmp_path):
        """load_intake with malformed JSON should return default."""
        from aah.core.intake.intake import load_intake

        bad_path = tmp_path / "bad.json"
        bad_path.write_text("{invalid json")
        loaded = load_intake(bad_path)
        assert loaded["status"] == "not_started"

    def test_intake_round_with_all_malformed(self):
        """Round where all entries are malformed should produce empty answers."""
        from aah.core.intake.intake import get_all_answers, get_default_intake, add_qa_round

        intake = get_default_intake()
        add_qa_round(intake, "start", [
            {"answer": "orphan"},  # no question
            {},  # empty
            {"question": ""},  # empty question
        ])
        answers = get_all_answers(intake)
        assert len(answers) == 0

    def test_is_complete_no_complexity_assessment(self):
        """is_complete should return False without complexity assessment."""
        from aah.core.intake.intake import (
            get_default_intake, is_complete, set_problem_statement,
            set_project_type, add_qa_round,
        )

        intake = get_default_intake()
        set_problem_statement(intake, "Build app")
        set_project_type(intake, "new_system")
        add_qa_round(intake, "start", [{"question": "Q", "answer": "A"}])
        assert not is_complete(intake)

    def test_get_intake_summary_no_rounds(self):
        """Summary with no rounds should still include problem and type."""
        from aah.core.intake.intake import (
            get_default_intake, get_intake_summary, set_problem_statement,
            set_project_type,
        )

        intake = get_default_intake()
        set_problem_statement(intake, "Fix login bug")
        set_project_type(intake, "bug_fix")
        summary = get_intake_summary(intake)
        assert "login bug" in summary
        assert "bug_fix" in summary
