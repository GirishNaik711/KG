"""Tests for aah.core.intake modules (intake, questionnaires, classifier)."""

import json
from pathlib import Path

import pytest

from aah.core.intake.intake import (
    add_qa_round,
    get_all_answers,
    get_answered_topics,
    get_default_intake,
    get_intake_summary,
    is_complete,
    load_intake,
    mark_complete,
    save_intake,
    set_complexity_assessment,
    set_phase_plan,
    set_problem_statement,
    set_project_type,
    set_raw_context,
)
from aah.core.intake.classifier import (
    classify_complexity,
    compute_phase_plan,
    is_trivial,
)


# ─── intake.py ──────────────────────────────────────────────────────


class TestIntakeDataModel:
    def test_default_intake(self):
        intake = get_default_intake()
        assert intake["version"] == "1.0"
        assert intake["status"] == "not_started"
        assert intake["problem_statement"] is None
        assert intake["rounds"] == []

    def test_set_problem_statement(self):
        intake = get_default_intake()
        set_problem_statement(intake, "Build a task manager API")
        assert intake["problem_statement"] == "Build a task manager API"
        assert intake["status"] == "in_progress"

    def test_set_project_type(self):
        intake = get_default_intake()
        set_project_type(intake, "new_system")
        assert intake["project_type"] == "new_system"

    def test_set_raw_context(self):
        intake = get_default_intake()
        context = "We need a multi-agent memory system with:\n- Postgres backend\n- Vector search\n- Universal API"
        set_raw_context(intake, context)
        assert intake["raw_context"] == context

    def test_raw_context_in_default_is_none(self):
        intake = get_default_intake()
        assert intake["raw_context"] is None

    def test_raw_context_persists(self, tmp_path):
        path = tmp_path / ".rapids" / "intake.json"
        intake = get_default_intake()
        set_raw_context(intake, "200 lines of project context here")
        save_intake(intake, path)
        loaded = load_intake(path)
        assert loaded["raw_context"] == "200 lines of project context here"

    def test_raw_context_in_summary(self):
        intake = get_default_intake()
        set_problem_statement(intake, "Build agent memory")
        set_raw_context(intake, "Detailed requirements:\n- Must support multi-tenant")
        summary = get_intake_summary(intake)
        assert "Detailed requirements" in summary
        assert "multi-tenant" in summary

    def test_set_project_type_invalid(self):
        intake = get_default_intake()
        with pytest.raises(ValueError):
            set_project_type(intake, "invalid_type")

    def test_add_qa_round(self):
        intake = get_default_intake()
        qa = [
            {"question": "What type?", "answer": "New system", "header": "Type"},
            {"question": "Integrations?", "answer": "1-2 services", "header": "Integration"},
        ]
        add_qa_round(intake, "start", qa)
        assert len(intake["rounds"]) == 1
        assert intake["rounds"][0]["phase"] == "start"
        assert len(intake["rounds"][0]["questions"]) == 2
        assert intake["status"] == "in_progress"

    def test_multiple_qa_rounds(self):
        intake = get_default_intake()
        add_qa_round(intake, "start", [{"question": "Q1", "answer": "A1"}])
        add_qa_round(intake, "research", [{"question": "Q2", "answer": "A2"}])
        assert len(intake["rounds"]) == 2

    def test_get_all_answers(self):
        intake = get_default_intake()
        add_qa_round(intake, "start", [
            {"question": "Q1", "answer": "A1"},
            {"question": "Q2", "answer": "A2"},
        ])
        add_qa_round(intake, "research", [
            {"question": "Q3", "answer": "A3"},
        ])
        answers = get_all_answers(intake)
        assert len(answers) == 3
        assert answers["Q1"] == "A1"
        assert answers["Q3"] == "A3"

    def test_get_answered_topics(self):
        intake = get_default_intake()
        add_qa_round(intake, "start", [
            {"question": "Q1", "answer": "A1", "header": "Type"},
            {"question": "Q2", "answer": "A2", "header": "Scope"},
        ])
        topics = get_answered_topics(intake)
        assert "Type" in topics
        assert "Scope" in topics


class TestIntakePersistence:
    def test_save_and_load(self, tmp_path):
        path = tmp_path / ".rapids" / "intake.json"
        intake = get_default_intake()
        set_problem_statement(intake, "Test problem")
        add_qa_round(intake, "start", [{"question": "Q", "answer": "A"}])
        save_intake(intake, path)

        loaded = load_intake(path)
        assert loaded["problem_statement"] == "Test problem"
        assert len(loaded["rounds"]) == 1

    def test_load_nonexistent(self):
        loaded = load_intake(Path("/nonexistent"))
        assert loaded["status"] == "not_started"


class TestIntakeCompleteness:
    def test_incomplete_no_problem(self):
        intake = get_default_intake()
        assert not is_complete(intake)

    def test_incomplete_no_type(self):
        intake = get_default_intake()
        set_problem_statement(intake, "Problem")
        assert not is_complete(intake)

    def test_incomplete_no_assessment(self):
        intake = get_default_intake()
        set_problem_statement(intake, "Problem")
        set_project_type(intake, "new_system")
        add_qa_round(intake, "start", [{"question": "Q", "answer": "A"}])
        assert not is_complete(intake)

    def test_complete_trivial(self):
        intake = get_default_intake()
        set_problem_statement(intake, "Fix login bug")
        set_project_type(intake, "bug_fix")
        add_qa_round(intake, "start", [{"question": "Q", "answer": "A"}])
        set_complexity_assessment(intake, {"tier": "trivial", "scope": "bug_fix"})
        assert is_complete(intake)

    def test_complete_significant_needs_2_rounds(self):
        intake = get_default_intake()
        set_problem_statement(intake, "Build new system")
        set_project_type(intake, "new_system")
        add_qa_round(intake, "start", [{"question": "Q1", "answer": "A1"}])
        set_complexity_assessment(intake, {"tier": "significant", "scope": "new_system"})
        assert not is_complete(intake)  # Needs 2 rounds

        add_qa_round(intake, "followup", [{"question": "Q2", "answer": "A2"}])
        assert is_complete(intake)

    def test_mark_complete_overrides(self):
        intake = get_default_intake()
        mark_complete(intake)
        assert is_complete(intake)


class TestIntakeSummary:
    def test_summary_text(self):
        intake = get_default_intake()
        set_problem_statement(intake, "Build a graph RAG app")
        set_project_type(intake, "new_system")
        set_complexity_assessment(intake, {"tier": "significant", "reasoning": "Complex domain"})
        add_qa_round(intake, "start", [
            {"question": "What domains?", "answer": "Data/ML, Web API"},
        ])

        summary = get_intake_summary(intake)
        assert "graph RAG" in summary
        assert "new_system" in summary
        assert "significant" in summary
        assert "Data/ML" in summary

    def test_summary_shows_all_qa_by_default(self):
        """Verify all Q&A entries are shown, not truncated to 10."""
        intake = get_default_intake()
        set_problem_statement(intake, "Big project")
        # Add 25+ Q&A entries across 3 phases
        add_qa_round(intake, "start", [
            {"question": f"Start Q{i}?", "answer": f"Answer {i}"} for i in range(10)
        ])
        add_qa_round(intake, "research", [
            {"question": f"Research Q{i}?", "answer": f"R-Answer {i}"} for i in range(10)
        ])
        add_qa_round(intake, "analysis", [
            {"question": f"Analysis Q{i}?", "answer": f"A-Answer {i}"} for i in range(8)
        ])

        summary = get_intake_summary(intake)
        # All 28 entries should be present (not truncated to 10)
        assert "Start Q9?" in summary
        assert "Research Q9?" in summary
        assert "Analysis Q7?" in summary
        # Phase headers should be present
        assert "[start]" in summary
        assert "[research]" in summary
        assert "[analysis]" in summary

    def test_summary_groups_by_phase(self):
        """Verify Q&A is grouped by phase."""
        intake = get_default_intake()
        set_problem_statement(intake, "Test project")
        add_qa_round(intake, "start", [
            {"question": "Q1?", "answer": "A1"},
        ])
        add_qa_round(intake, "research", [
            {"question": "Q2?", "answer": "A2"},
        ])

        summary = get_intake_summary(intake)
        # Phase headers should appear before their Q&A
        start_pos = summary.index("[start]")
        q1_pos = summary.index("Q1?")
        research_pos = summary.index("[research]")
        q2_pos = summary.index("Q2?")
        assert start_pos < q1_pos < research_pos < q2_pos

    def test_summary_max_entries_limits(self):
        """Verify max_entries parameter caps output."""
        intake = get_default_intake()
        set_problem_statement(intake, "Test project")
        add_qa_round(intake, "start", [
            {"question": f"Q{i}?", "answer": f"A{i}"} for i in range(20)
        ])

        summary = get_intake_summary(intake, max_entries=5)
        assert "Q4?" in summary  # 5th entry (0-indexed)
        assert "Q5?" not in summary  # 6th entry should be truncated
        assert "more entries" in summary

    def test_summary_handles_missing_answers(self):
        """Verify malformed Q&A entries don't crash the summary."""
        intake = get_default_intake()
        set_problem_statement(intake, "Test project")
        add_qa_round(intake, "start", [
            {"question": "Q1?"},  # missing answer
            {"answer": "orphan"},  # missing question
            {"question": "Q2?", "answer": "A2"},
        ])

        summary = get_intake_summary(intake)
        assert "Q2?" in summary
        # Malformed entries should be skipped, not crash
        assert "orphan" not in summary


# ─── classifier.py ──────────────────────────────────────────────────


class TestClassifier:
    def _make_intake(self, project_type="new_system", integration="1-2", rounds=2):
        intake = get_default_intake()
        set_problem_statement(intake, "Test project")
        set_project_type(intake, project_type)
        add_qa_round(intake, "start", [
            {"question": "How many external systems?", "answer": f"{integration} services"},
        ])
        if rounds > 1:
            add_qa_round(intake, "followup", [
                {"question": "Data landscape?", "answer": "Simple CRUD"},
            ])
        return intake

    def test_bug_fix_is_trivial(self):
        intake = self._make_intake(project_type="bug_fix", integration="None")
        assessment = classify_complexity(intake)
        assert assessment["tier"] == "trivial"
        assert assessment["scope"] == "bug_fix"

    def test_new_system_low_complexity(self):
        intake = self._make_intake(project_type="new_system", integration="1-2")
        assessment = classify_complexity(intake)
        assert assessment["tier"] in ("moderate", "significant")

    def test_high_integration_is_complex(self):
        intake = self._make_intake(project_type="new_system", integration="6+")
        assessment = classify_complexity(intake)
        assert assessment["tier"] in ("significant", "complex")

    def test_classification_has_all_fields(self):
        intake = self._make_intake()
        assessment = classify_complexity(intake)
        assert "scope" in assessment
        assert "ambiguity" in assessment
        assert "domain_complexity" in assessment
        assert "integration_surface" in assessment
        assert "tier" in assessment
        assert "reasoning" in assessment


class TestPhasePlan:
    def test_trivial_skips_phases(self):
        plan = compute_phase_plan("trivial")
        assert not plan["research"]["required"]
        assert not plan["analysis"]["required"]
        assert plan["implement"]["required"]

    def test_complex_all_phases(self):
        plan = compute_phase_plan("complex")
        for phase, cfg in plan.items():
            assert cfg["required"] is True
            assert cfg["depth"] == "full"

    def test_bug_fix_overrides(self):
        plan = compute_phase_plan("moderate", "bug_fix")
        assert not plan["research"]["required"]
        assert not plan["analysis"]["required"]

    def test_moderate_standard_depth(self):
        plan = compute_phase_plan("moderate")
        assert plan["research"]["depth"] == "lightweight"
        assert plan["analysis"]["depth"] == "standard"
        assert plan["implement"]["depth"] == "standard"


class TestIsTrivial:
    def test_bug_fix_low_integration(self):
        intake = get_default_intake()
        set_project_type(intake, "bug_fix")
        add_qa_round(intake, "start", [
            {"question": "How many external systems?", "answer": "None — standalone"},
        ])
        assert is_trivial(intake)

    def test_bug_fix_high_integration(self):
        intake = get_default_intake()
        set_project_type(intake, "bug_fix")
        add_qa_round(intake, "start", [
            {"question": "How many external systems?", "answer": "6+ services"},
        ])
        assert not is_trivial(intake)

    def test_new_system_not_trivial(self):
        intake = get_default_intake()
        set_project_type(intake, "new_system")
        assert not is_trivial(intake)

    def test_trivial_from_assessment(self):
        intake = get_default_intake()
        set_complexity_assessment(intake, {"tier": "trivial"})
        assert is_trivial(intake)
