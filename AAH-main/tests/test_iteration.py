"""Tests for aah.core.iteration.manager."""

from pathlib import Path

import pytest

from aah.core.common.io_utils import read_json, read_yaml, write_json, write_yaml
from aah.core.common.manifest import get_default_manifest, save_manifest
from aah.core.iteration.manager import (
    create_new_iteration,
    detect_needs_new_iteration,
    get_completed_iterations,
    get_current_iteration,
    get_iteration_dir,
    get_iteration_summary,
)


@pytest.fixture
def rapids_project(tmp_path):
    """Create a minimal .rapids/ structure."""
    rapids = tmp_path / ".rapids"
    for d in ["plan/features", "plan/specs", "plan/sprint-contracts",
              "implement/test-results", "implement/wave-summaries",
              "iterations", "audit", "research", "analysis/decisions"]:
        (rapids / d).mkdir(parents=True)
    manifest = get_default_manifest("test-project")
    save_manifest(manifest, rapids / "manifest.yaml")
    return rapids


class TestGetIterationDir:
    def test_returns_correct_path(self, rapids_project):
        path = get_iteration_dir(rapids_project, 1)
        assert path == rapids_project / "iterations" / "iter-1"

    def test_iteration_numbers(self, rapids_project):
        assert get_iteration_dir(rapids_project, 3).name == "iter-3"


class TestGetCurrentIteration:
    def test_default_is_1(self, rapids_project):
        assert get_current_iteration(rapids_project) == 1

    def test_reads_from_manifest(self, rapids_project):
        manifest = get_default_manifest("test")
        manifest["current_iteration"] = 3
        save_manifest(manifest, rapids_project / "manifest.yaml")
        assert get_current_iteration(rapids_project) == 3


class TestDetectNeedsNewIteration:
    def test_init_phase_no_new_needed(self, rapids_project):
        result = detect_needs_new_iteration(rapids_project)
        assert not result["needs_new"]

    def test_implement_phase_no_new_needed(self, rapids_project):
        manifest = get_default_manifest("test")
        manifest["current_phase"] = "implement"
        save_manifest(manifest, rapids_project / "manifest.yaml")
        result = detect_needs_new_iteration(rapids_project)
        assert not result["needs_new"]

    def test_complete_phase_needs_new(self, rapids_project):
        manifest = get_default_manifest("test")
        manifest["current_phase"] = "complete"
        save_manifest(manifest, rapids_project / "manifest.yaml")
        write_json({"features": [
            {"id": "F001", "passes": True},
            {"id": "F002", "passes": True},
        ]}, rapids_project / "feature-list.json")
        result = detect_needs_new_iteration(rapids_project)
        assert result["needs_new"]
        assert result["passing_features"] == 2

    def test_deploy_phase_needs_new(self, rapids_project):
        manifest = get_default_manifest("test")
        manifest["current_phase"] = "deploy"
        save_manifest(manifest, rapids_project / "manifest.yaml")
        result = detect_needs_new_iteration(rapids_project)
        assert result["needs_new"]


class TestCreateNewIteration:
    def test_no_work_stays_at_iter_1(self, rapids_project):
        """No active work → stays at iteration 1, no iter-* dir created."""
        result = create_new_iteration(rapids_project, "First task")
        assert result["iteration"] == 1
        # No iter-1/ should exist — it's only created at archival time
        assert not get_iteration_dir(rapids_project, 1).exists()

    def test_no_iter_dir_created_during_first_iteration(self, rapids_project):
        """During iteration 1 work, no iter-* directories should exist."""
        # Add features (simulating active iteration 1 work)
        write_yaml({"id": "F001", "description": "Auth"},
                    rapids_project / "plan" / "features" / "F001.yaml")
        # iter-1/ should NOT exist — it's only created when archiving
        assert not get_iteration_dir(rapids_project, 1).exists()

    def test_archives_to_iter1_when_starting_iter2(self, rapids_project):
        """Starting iter-2 archives active state into iter-1/."""
        # Simulate iteration 1 work
        write_yaml({"id": "F001", "description": "Auth"},
                    rapids_project / "plan" / "features" / "F001.yaml")
        write_yaml({"id": "F002", "description": "Tasks"},
                    rapids_project / "plan" / "features" / "F002.yaml")
        write_json({"problem_statement": "Build API"}, rapids_project / "intake.json")
        write_json({"features": [
            {"id": "F001", "passes": True, "description": "Auth"},
        ]}, rapids_project / "feature-list.json")

        result = create_new_iteration(rapids_project, "Add notifications")
        assert result["iteration"] == 2

        # iter-1/ should now exist with archived data
        iter1 = get_iteration_dir(rapids_project, 1)
        assert iter1.is_dir()
        assert (iter1 / "features" / "F001.yaml").exists()
        assert (iter1 / "features" / "F002.yaml").exists()
        assert (iter1 / "intake.json").exists()
        assert (iter1 / "feature-list-snapshot.json").exists()
        assert (iter1 / "status.json").exists()

        status = read_json(iter1 / "status.json")
        assert status["status"] == "complete"
        assert status["iteration"] == 1

    def test_clears_active_state_after_archival(self, rapids_project):
        """Active plan/implement/intake are cleared for the new iteration."""
        write_yaml({"id": "F001", "description": "Auth"},
                    rapids_project / "plan" / "features" / "F001.yaml")
        write_json({"problem_statement": "Build API"}, rapids_project / "intake.json")
        (rapids_project / "implement" / "impl-state.json").write_text("{}")
        (rapids_project / "plan" / "dag.json").write_text("{}")

        create_new_iteration(rapids_project, "Next phase")

        # Active dirs should be cleared
        assert not list((rapids_project / "plan" / "features").glob("*.yaml"))
        assert not (rapids_project / "plan" / "dag.json").exists()
        assert not (rapids_project / "intake.json").exists()
        assert not (rapids_project / "implement" / "impl-state.json").exists()

    def test_preserves_shared_artifacts(self, rapids_project):
        """Shared artifacts (research, analysis, feature-list.json) survive."""
        write_yaml({"id": "F001", "description": "Auth"},
                    rapids_project / "plan" / "features" / "F001.yaml")
        (rapids_project / "research" / "constraints.md").write_text("# Constraints")
        (rapids_project / "analysis" / "nfr.md").write_text("# NFR")
        write_json({"features": [{"id": "F001", "passes": True}]},
                    rapids_project / "feature-list.json")

        create_new_iteration(rapids_project, "Iter 2")

        # Shared artifacts remain
        assert (rapids_project / "research" / "constraints.md").exists()
        assert (rapids_project / "analysis" / "nfr.md").exists()
        assert (rapids_project / "feature-list.json").exists()

    def test_generates_prior_iteration_context(self, rapids_project):
        """prior-iteration-context.md is generated for iter-2+ to reference."""
        write_yaml({"id": "F001", "description": "Auth"},
                    rapids_project / "plan" / "features" / "F001.yaml")
        write_json({"problem_statement": "Build API"}, rapids_project / "intake.json")
        write_json({"features": [
            {"id": "F001", "passes": True, "description": "Auth"},
        ]}, rapids_project / "feature-list.json")

        create_new_iteration(rapids_project, "Add notifications")

        ctx_path = rapids_project / "prior-iteration-context.md"
        assert ctx_path.exists()
        content = ctx_path.read_text()
        assert "Iteration 1" in content
        assert "Build API" in content
        assert "F001" in content

    def test_updates_manifest(self, rapids_project):
        """Manifest is updated with new iteration number and phase reset."""
        write_yaml({"id": "F001", "description": "Auth"},
                    rapids_project / "plan" / "features" / "F001.yaml")

        create_new_iteration(rapids_project, "Iter 2")

        manifest = read_yaml(rapids_project / "manifest.yaml")
        assert manifest["current_iteration"] == 2
        assert manifest["current_phase"] == "init"

    def test_archives_implement_artifacts(self, rapids_project):
        """Implementation artifacts are archived into iter-N/."""
        write_yaml({"id": "F001", "description": "Auth"},
                    rapids_project / "plan" / "features" / "F001.yaml")
        (rapids_project / "implement" / "impl-state.json").write_text('{"status": "done"}')
        (rapids_project / "implement" / "test-results" / "F001.json").write_text('{"pass": true}')
        (rapids_project / "implement" / "wave-summaries" / "wave-1.md").write_text("# Wave 1")

        create_new_iteration(rapids_project, "Iter 2")

        iter1 = get_iteration_dir(rapids_project, 1)
        assert (iter1 / "implement" / "impl-state.json").exists()
        assert (iter1 / "implement" / "test-results" / "F001.json").exists()
        assert (iter1 / "implement" / "wave-summaries" / "wave-1.md").exists()

    def test_iter3_archives_iter2_uniformly(self, rapids_project):
        """Iter-3 creation archives iter-2 the same way as iter-2 archived iter-1."""
        # Iter-1 work → start iter-2
        write_yaml({"id": "F001", "description": "Auth"},
                    rapids_project / "plan" / "features" / "F001.yaml")
        create_new_iteration(rapids_project, "Iter 2")

        # Iter-2 work → start iter-3
        write_yaml({"id": "F003", "description": "Notifications"},
                    rapids_project / "plan" / "features" / "F003.yaml")
        write_json({"problem_statement": "Add notifications"}, rapids_project / "intake.json")
        create_new_iteration(rapids_project, "Iter 3")

        # Both archives should exist
        assert get_iteration_dir(rapids_project, 1).is_dir()
        assert get_iteration_dir(rapids_project, 2).is_dir()
        assert (get_iteration_dir(rapids_project, 2) / "features" / "F003.yaml").exists()
        assert (get_iteration_dir(rapids_project, 2) / "intake.json").exists()

        # Active dirs should be clean
        assert not list((rapids_project / "plan" / "features").glob("*.yaml"))
        assert not (rapids_project / "intake.json").exists()

        # Manifest at iter 3
        manifest = read_yaml(rapids_project / "manifest.yaml")
        assert manifest["current_iteration"] == 3

    def test_syncs_iteration_history(self, rapids_project):
        """iteration-history.yaml is updated on new iteration."""
        write_yaml({"id": "F001", "description": "Auth"},
                    rapids_project / "plan" / "features" / "F001.yaml")
        create_new_iteration(rapids_project, "Iter 2")

        history_path = rapids_project / "iteration-history.yaml"
        assert history_path.exists()
        history = read_yaml(history_path)
        iterations = history["iterations"]
        # iter-1 completed + iter-2 in_progress
        assert len(iterations) == 2
        assert iterations[0]["status"] == "completed"
        assert iterations[0]["number"] == 1
        assert iterations[1]["status"] == "in_progress"
        assert iterations[1]["number"] == 2

    def test_feature_list_snapshot_in_archive(self, rapids_project):
        """Archive contains a snapshot of feature-list.json at archival time."""
        write_yaml({"id": "F001", "description": "Auth"},
                    rapids_project / "plan" / "features" / "F001.yaml")
        write_json({"features": [
            {"id": "F001", "passes": True, "description": "Auth"},
            {"id": "F002", "passes": False, "description": "Tasks"},
        ]}, rapids_project / "feature-list.json")

        create_new_iteration(rapids_project, "Iter 2")

        snapshot = read_json(get_iteration_dir(rapids_project, 1) / "feature-list-snapshot.json")
        assert len(snapshot["features"]) == 2
        assert snapshot["features"][0]["passes"] is True


class TestGetCompletedIterations:
    def test_no_iterations(self, rapids_project):
        assert get_completed_iterations(rapids_project) == []

    def test_with_completed(self, rapids_project):
        iter1 = get_iteration_dir(rapids_project, 1)
        iter1.mkdir(parents=True)
        write_json({"status": "complete"}, iter1 / "status.json")

        iter2 = get_iteration_dir(rapids_project, 2)
        iter2.mkdir(parents=True)
        write_json({"status": "in_progress"}, iter2 / "status.json")

        completed = get_completed_iterations(rapids_project)
        assert completed == [1]


class TestIterationSummary:
    def test_summary_with_iterations(self, rapids_project):
        iter1 = get_iteration_dir(rapids_project, 1)
        iter1.mkdir(parents=True)
        (iter1 / "features").mkdir()
        write_yaml({"id": "F001"}, iter1 / "features" / "F001.yaml")
        write_json({
            "iteration": 1, "status": "complete",
            "problem_statement": "Build API", "started_at": "2026-01-01",
        }, iter1 / "status.json")

        manifest = get_default_manifest("test")
        manifest["current_iteration"] = 2
        save_manifest(manifest, rapids_project / "manifest.yaml")

        summary = get_iteration_summary(rapids_project)
        assert summary["current_iteration"] == 2
        assert len(summary["iterations"]) == 1
        assert summary["iterations"][0]["status"] == "complete"
        assert summary["iterations"][0]["features"] == 1
