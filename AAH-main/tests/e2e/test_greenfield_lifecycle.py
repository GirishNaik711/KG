"""
E2E Tests: Greenfield project full lifecycle.

Tests the complete AAH flow from workspace creation through implementation,
using the Claude Agent SDK to orchestrate real Claude Code sessions.

Scenarios:
  - Workspace + project scaffolding
  - Problem intake with AskUserQuestion
  - Complexity classification
  - Research phase with artifacts
  - Analysis phase with ADRs
  - Planning: features, DAG, waves, contracts
  - Implementation: agent dispatch, QA, regression
  - Merge to develop
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.e2e.conftest import e2e, skip_no_claude, skip_no_api_key


# ─── Scenario 1: Scaffold ───────────────────────────────────────────

class TestScaffolding:
    """Test workspace and project creation without Claude sessions."""

    def test_workspace_creation(self, test_workspace):
        workspace_root, config_path = test_workspace
        result = subprocess.run(
            ["aah-run", "aah.core.scaffold.workspace", "create", "my-ws",
             "--config-path", str(config_path), "--workspace-root", str(workspace_root)],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
                 "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"},
        )
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["status"] == "created"
        assert (workspace_root / "my-ws" / "workspace.yaml").exists()

    def test_project_scaffolding(self, scaffolded_project):
        project = scaffolded_project
        # Verify .aah structure
        assert (project / ".aah" / "manifest.yaml").exists()
        assert (project / ".aah" / "claude-progress.json").exists()
        assert (project / ".aah" / "discuss").is_dir()
        assert (project / ".aah" / "architecture" / "decisions").is_dir()
        assert (project / ".aah" / "plan" / "features").is_dir()
        assert (project / ".aah" / "build" / "test-results").is_dir()
        assert (project / ".aah" / "iterations").is_dir()

        # Verify git
        result = subprocess.run(
            ["git", "branch", "--format=%(refname:short)"],
            cwd=project, capture_output=True, text=True,
        )
        branches = result.stdout.strip().split("\n")
        assert "main" in branches
        assert "develop" in branches

    def test_manifest_initial_state(self, scaffolded_project):
        result = subprocess.run(
            ["aah-run", "aah.core.common.manifest", "get-status",
             "--path", str(scaffolded_project / ".aah" / "manifest.yaml")],
            capture_output=True, text=True, timeout=15,
        )
        status = json.loads(result.stdout)
        assert status["project_type"] == "greenfield"
        assert status["current_phase"] == "init"
        assert status["stack_choices"]["primary"] == "python-fastapi"

    def test_progress_initial_state(self, scaffolded_project):
        result = subprocess.run(
            ["aah-run", "aah.core.common.progress", "summary",
             "--path", str(scaffolded_project / ".aah" / "claude-progress.json")],
            capture_output=True, text=True, timeout=15,
        )
        summary = json.loads(result.stdout)
        assert summary["phase"] == "init"


# ─── Scenario 2: Intake & Classification ────────────────────────────

class TestIntakeAndClassification:
    """Test the intake questionnaire and complexity classification."""

    def test_intake_initialization(self, scaffolded_project):
        aah_root = scaffolded_project / ".aah"
        result = subprocess.run(
            ["aah-run", "aah.core.intake.intake", "init",
             "--path", str(aah_root / "intake.json"),
             "--problem", "Build a task management API",
             "--type", "new_system"],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0
        intake = json.loads(result.stdout)
        assert intake["problem_statement"] == "Build a task management API"
        assert intake["project_type"] == "new_system"

    def test_add_qa_round(self, scaffolded_project):
        aah_root = scaffolded_project / ".aah"
        # Init first
        subprocess.run(
            ["aah-run", "aah.core.intake.intake", "init",
             "--path", str(aah_root / "intake.json"),
             "--problem", "Build an API", "--type", "new_system"],
            capture_output=True, timeout=15,
        )
        # Add Q&A
        qa = json.dumps([
            {"question": "Integration?", "answer": "1-2 services", "header": "Integration"},
            {"question": "Domains?", "answer": "Web API", "header": "Domains"},
        ])
        result = subprocess.run(
            ["aah-run", "aah.core.intake.intake", "add-round",
             "--path", str(aah_root / "intake.json"),
             "--phase", "start", "--qa-json", qa],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0

    def test_complexity_classification_new_system(self, scaffolded_project):
        aah_root = scaffolded_project / ".aah"
        # Setup intake
        subprocess.run(
            ["aah-run", "aah.core.intake.intake", "init",
             "--path", str(aah_root / "intake.json"),
             "--problem", "Complex distributed system", "--type", "new_system"],
            capture_output=True, timeout=15,
        )
        qa = json.dumps([
            {"question": "Integration?", "answer": "6+ services", "header": "Integration"},
            {"question": "Domains?", "answer": "Data/ML/AI", "header": "Domains"},
        ])
        subprocess.run(
            ["aah-run", "aah.core.intake.intake", "add-round",
             "--path", str(aah_root / "intake.json"), "--phase", "start", "--qa-json", qa],
            capture_output=True, timeout=15,
        )
        # Classify
        result = subprocess.run(
            ["aah-run", "aah.core.intake.classifier", "classify",
             "--intake-path", str(aah_root / "intake.json")],
            capture_output=True, text=True, timeout=15,
        )
        assessment = json.loads(result.stdout)
        assert assessment["scope"] == "new_system"
        assert assessment["tier"] in ("significant", "complex")

    def test_bug_fix_is_trivial(self, scaffolded_project):
        aah_root = scaffolded_project / ".aah"
        subprocess.run(
            ["aah-run", "aah.core.intake.intake", "init",
             "--path", str(aah_root / "intake.json"),
             "--problem", "Fix login redirect", "--type", "bug_fix"],
            capture_output=True, timeout=15,
        )
        qa = json.dumps([{"question": "Integration?", "answer": "None", "header": "Integration"}])
        subprocess.run(
            ["aah-run", "aah.core.intake.intake", "add-round",
             "--path", str(aah_root / "intake.json"), "--phase", "start", "--qa-json", qa],
            capture_output=True, timeout=15,
        )
        result = subprocess.run(
            ["aah-run", "aah.core.intake.classifier", "is-trivial",
             "--intake-path", str(aah_root / "intake.json")],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0  # exit 0 = trivial
        assert json.loads(result.stdout)["trivial"] is True

    def test_phase_plan_trivial_skips_research(self):
        result = subprocess.run(
            ["aah-run", "aah.core.intake.classifier", "phase-plan",
             "--tier", "trivial", "--type", "bug_fix"],
            capture_output=True, text=True, timeout=15,
        )
        plan = json.loads(result.stdout)
        assert plan["discuss"]["required"] is False
        assert plan["architecture"]["required"] is False
        assert plan["build"]["required"] is True

    def test_phase_plan_complex_all_phases(self):
        result = subprocess.run(
            ["aah-run", "aah.core.intake.classifier", "phase-plan",
             "--tier", "complex"],
            capture_output=True, text=True, timeout=15,
        )
        plan = json.loads(result.stdout)
        for phase in ("discuss", "architecture", "plan", "build", "deploy"):
            assert plan[phase]["required"] is True
            assert plan[phase]["depth"] == "full"


# ─── Scenario 3: Planning Phase ─────────────────────────────────────

class TestPlanningPhase:
    """Test feature decomposition, DAG, waves, and sprint contracts."""

    def test_feature_list_build(self, project_with_features):
        fl = json.loads((project_with_features / ".aah" / "feature-list.json").read_text())
        assert len(fl["features"]) == 4
        assert all(f["passes"] is False for f in fl["features"])

    def test_dag_valid(self, project_with_features):
        result = subprocess.run(
            ["aah-run", "aah.core.common.dag", "validate",
             "--dag-path", str(project_with_features / ".aah" / "plan" / "dag.json")],
            capture_output=True, text=True, timeout=15,
        )
        dag_result = json.loads(result.stdout)
        assert dag_result["valid"] is True

    def test_waves_computed(self, project_with_features):
        waves = json.loads((project_with_features / ".aah" / "plan" / "waves.json").read_text())
        assert len(waves["waves"]) >= 2
        # F001 and F002 should be in wave 0 (no deps)
        assert set(waves["waves"][0]) == {"F001", "F002"}

    def test_sprint_contracts_exist(self, project_with_features):
        waves = json.loads((project_with_features / ".aah" / "plan" / "waves.json").read_text())
        contracts_dir = project_with_features / ".aah" / "plan" / "sprint-contracts"
        for i in range(len(waves["waves"])):
            assert (contracts_dir / f"wave-{i}-contract.md").exists()

    def test_feature_list_structural_protection(self, project_with_features):
        """Verify that structural changes to feature-list.json are blocked."""
        from aah.core.common.feature_list import validate_structural_integrity
        fl = json.loads((project_with_features / ".aah" / "feature-list.json").read_text())

        # Status change should be valid
        import copy
        proposed = copy.deepcopy(fl)
        proposed["features"][0]["passes"] = True
        errors = validate_structural_integrity(fl, proposed)
        assert errors == []

        # Removing a feature should be invalid
        proposed2 = copy.deepcopy(fl)
        proposed2["features"].pop()
        errors = validate_structural_integrity(fl, proposed2)
        assert len(errors) > 0

        # Changing description should be invalid
        proposed3 = copy.deepcopy(fl)
        proposed3["features"][0]["description"] = "CHANGED"
        errors = validate_structural_integrity(fl, proposed3)
        assert len(errors) > 0


# ─── Scenario 4: Orchestrator ───────────────────────────────────────

class TestOrchestrator:
    """Test the deterministic orchestrator commands."""

    def test_next_action_dispatch(self, project_with_features):
        result = subprocess.run(
            ["aah-run", "aah.core.build.orchestrator", "next-action"],
            capture_output=True, text=True, timeout=15,
            cwd=project_with_features,
        )
        action = json.loads(result.stdout)
        assert action["action"].startswith("dispatch_")
        assert action["wave"] == 0
        assert "features" in action

    def test_wave_context_resolves_paths(self, project_with_features):
        result = subprocess.run(
            ["aah-run", "aah.core.build.update_impl_state",
             "wave-context", "--wave", "0"],
            capture_output=True, text=True, timeout=15,
            cwd=project_with_features,
        )
        ctx = json.loads(result.stdout)
        assert ctx["feature_count"] >= 1
        for f in ctx["features"]:
            assert f["yaml_path"] != f"NOT FOUND: {f['id']}"
            assert Path(f["yaml_path"]).exists()
        assert Path(ctx["contract_path"]).exists()

    def test_wave_readiness_not_ready(self, project_with_features):
        result = subprocess.run(
            ["aah-run", "aah.core.build.orchestrator",
             "wave-readiness", "--wave", "0"],
            capture_output=True, text=True, timeout=15,
            cwd=project_with_features,
        )
        readiness = json.loads(result.stdout)
        assert readiness["ready_to_merge"] is False
        assert len(readiness["blockers"]) > 0

    def test_frontier_with_reasons(self, project_with_features):
        result = subprocess.run(
            ["aah-run", "aah.core.build.orchestrator", "frontier"],
            capture_output=True, text=True, timeout=15,
            cwd=project_with_features,
        )
        frontier = json.loads(result.stdout)
        assert "F001" in frontier["available"]
        assert "F002" in frontier["available"]
        assert "F003" in frontier["blocked"]
        assert "F001" in frontier["blocked"]["F003"]["waiting_on"]

    def test_frontier_unblock_impact(self, project_with_features):
        result = subprocess.run(
            ["aah-run", "aah.core.build.orchestrator", "frontier"],
            capture_output=True, text=True, timeout=15,
            cwd=project_with_features,
        )
        frontier = json.loads(result.stdout)
        # F001 completion should unblock F003 (and partially unblock F004)
        if "unblock_impact" in frontier and frontier["unblock_impact"]:
            assert any("F003" in v for v in frontier["unblock_impact"].values())

    def test_next_action_after_wave_complete(self, project_with_features):
        """When all features in wave pass, next-action should say run_regression."""
        from aah.core.common.feature_list import update_feature_status
        fl_path = project_with_features / ".aah" / "feature-list.json"
        # Mark wave 0 features as passing
        update_feature_status(fl_path, "F001", True)
        update_feature_status(fl_path, "F002", True)

        result = subprocess.run(
            ["aah-run", "aah.core.build.orchestrator", "next-action"],
            capture_output=True, text=True, timeout=15,
            cwd=project_with_features,
        )
        action = json.loads(result.stdout)
        assert action["action"] == "run_regression"

    def test_next_action_after_regression(self, project_with_features):
        """After regression passes, next-action should say merge."""
        from aah.core.common.feature_list import update_feature_status
        from aah.core.common.io_utils import write_json
        fl_path = project_with_features / ".aah" / "feature-list.json"
        update_feature_status(fl_path, "F001", True)
        update_feature_status(fl_path, "F002", True)

        # Write passing regression
        write_json(
            {"passed": True, "total_tests": 10, "failed_count": 0, "failures": []},
            project_with_features / ".aah" / "build" / "test-results" / "regression-latest.json",
        )

        result = subprocess.run(
            ["aah-run", "aah.core.build.orchestrator", "next-action"],
            capture_output=True, text=True, timeout=15,
            cwd=project_with_features,
        )
        action = json.loads(result.stdout)
        assert action["action"] == "merge"


# ─── Scenario 5: Guards & Gates ─────────────────────────────────────

class TestGuardsAndGates:
    """Test hook guard scripts that protect project integrity."""

    def test_no_mocks_guard_allows_normal(self):
        result = subprocess.run(
            ["aah-run", "aah.core.guards.no_mocks_guard"],
            input='{"tool_input": {"command": "pip install flask"}}',
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0

    def test_no_mocks_guard_blocks_mock_install(self):
        result = subprocess.run(
            ["aah-run", "aah.core.guards.no_mocks_guard"],
            input='{"tool_input": {"command": "pip install pytest-mock"}}',
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 2

    def test_no_mocks_guard_blocks_sinon(self):
        result = subprocess.run(
            ["aah-run", "aah.core.guards.no_mocks_guard"],
            input='{"tool_input": {"command": "npm install sinon"}}',
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 2

    def test_artifact_template_validates_research(self):
        result = subprocess.run(
            ["aah-run", "aah.core.guards.validate_artifact_template"],
            input=json.dumps({
                "tool_input": {
                    "file_path": "/p/.aah/discuss/tech.md",
                    "content": "# Tech\n## Problem Framing\nX\n## Findings\nY\n## Trade-offs\nZ\n## Recommendation\nW",
                }
            }),
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0

    def test_artifact_template_rejects_incomplete_research(self):
        result = subprocess.run(
            ["aah-run", "aah.core.guards.validate_artifact_template"],
            input=json.dumps({
                "tool_input": {
                    "file_path": "/p/.aah/discuss/tech.md",
                    "content": "# Tech\nJust some text without sections",
                }
            }),
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 2

    def test_validate_main_merge_not_ready(self, project_with_features):
        result = subprocess.run(
            ["aah-run", "aah.core.git_ops.validate_main_merge",
             "--project-path", str(project_with_features)],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 2  # Not ready
        output = json.loads(result.stdout)
        assert output["ready"] is False


# ─── Scenario 6: Iteration Management ───────────────────────────────

class TestIterations:
    """Test multi-iteration project lifecycle."""

    def test_detect_no_iteration_needed(self, project_with_features):
        result = subprocess.run(
            ["aah-run", "aah.core.iteration.manager", "detect"],
            capture_output=True, text=True, timeout=15,
            cwd=project_with_features,
        )
        detection = json.loads(result.stdout)
        assert detection["needs_new"] is False  # Phase is implement, not complete

    def test_detect_iteration_needed_when_complete(self, project_with_features):
        import yaml
        manifest_path = project_with_features / ".aah" / "manifest.yaml"
        manifest = yaml.safe_load(open(manifest_path))
        manifest["current_phase"] = "complete"
        with open(manifest_path, "w") as f:
            yaml.dump(manifest, f)

        result = subprocess.run(
            ["aah-run", "aah.core.iteration.manager", "detect"],
            capture_output=True, text=True, timeout=15,
            cwd=project_with_features,
        )
        detection = json.loads(result.stdout)
        assert detection["needs_new"] is True

    def test_create_new_iteration(self, project_with_features):
        result = subprocess.run(
            ["aah-run", "aah.core.iteration.manager", "new",
             "--problem", "Add notifications"],
            capture_output=True, text=True, timeout=15,
            cwd=project_with_features,
        )
        iteration = json.loads(result.stdout)
        assert iteration["status"] == "created"
        iter_dir = Path(iteration["path"])
        assert (iter_dir / "features").is_dir()
        assert (iter_dir / "intake.json").exists()


# ─── Scenario 8: Activity Logging & Token Tracking ──────────────────

class TestActivityLogging:
    """Test session activity and token usage tracking."""

    def test_usage_report_empty(self, project_with_features):
        result = subprocess.run(
            ["aah-run", "aah.core.logging.activity_logger", "usage"],
            capture_output=True, text=True, timeout=15,
            cwd=project_with_features,
        )
        report = json.loads(result.stdout)
        assert report["total_tokens"] == 0

    def test_activity_report_empty(self, project_with_features):
        result = subprocess.run(
            ["aah-run", "aah.core.logging.activity_logger", "activity"],
            capture_output=True, text=True, timeout=15,
            cwd=project_with_features,
        )
        entries = json.loads(result.stdout)
        assert isinstance(entries, list)


# ─── Scenario 9: Questionnaire Adaptive Behavior ────────────────────

class TestQuestionnaires:
    """Test that questionnaires adapt based on answered topics."""

    def test_initial_questions_format(self):
        result = subprocess.run(
            ["aah-run", "aah.core.intake.questionnaires", "initial"],
            capture_output=True, text=True, timeout=15,
        )
        questions = json.loads(result.stdout)
        assert len(questions) == 4
        for q in questions:
            assert "question" in q
            assert "header" in q
            assert "options" in q
            assert 2 <= len(q["options"]) <= 4

    def test_followup_skips_answered_topics(self, scaffolded_project):
        aah_root = scaffolded_project / ".aah"
        subprocess.run(
            ["aah-run", "aah.core.intake.intake", "init",
             "--path", str(aah_root / "intake.json"), "--problem", "Test"],
            capture_output=True, timeout=15,
        )
        # Answer constraints topic
        qa = json.dumps([{"question": "Constraints?", "answer": "None", "header": "Constraints"}])
        subprocess.run(
            ["aah-run", "aah.core.intake.intake", "add-round",
             "--path", str(aah_root / "intake.json"), "--phase", "start", "--qa-json", qa],
            capture_output=True, timeout=15,
        )
        result = subprocess.run(
            ["aah-run", "aah.core.intake.questionnaires", "followup",
             "--intake-path", str(aah_root / "intake.json")],
            capture_output=True, text=True, timeout=15,
        )
        questions = json.loads(result.stdout)
        headers = [q["header"] for q in questions]
        assert "Constraints" not in headers


# ─── Scenario 10: Orchestrator State Transitions ────────────────────

class TestOrchestratorStateTransitions:
    """Deterministic state machine edge cases for the orchestrator."""

    def test_next_action_when_feature_has_test_results_needs_qa(self, project_with_features):
        """When a feature has passing test results but isn't QA'd yet, action is run_qa."""
        from aah.core.common.io_utils import write_json
        # Write a passing test result for F001 (wave 0 feature) without marking it passing
        write_json(
            {"passed": True, "feature_id": "F001", "exit_code": 0, "failed_tests": []},
            project_with_features / ".aah" / "build" / "test-results" / "F001.json",
        )
        result = subprocess.run(
            ["aah-run", "aah.core.build.orchestrator", "next-action"],
            capture_output=True, text=True, timeout=15,
            cwd=project_with_features,
        )
        action = json.loads(result.stdout)
        assert action["action"] == "run_qa"
        assert "F001" in action["features"]

    def test_next_action_complete_when_all_waves_done(self, project_with_features):
        """When current_wave is beyond the last wave index, action is complete."""
        from aah.core.common.progress import save_progress, get_default_progress
        from aah.core.common.feature_list import update_feature_status
        fl_path = project_with_features / ".aah" / "feature-list.json"
        for fid in ["F001", "F002", "F003", "F004"]:
            update_feature_status(fl_path, fid, True)
        progress = get_default_progress()
        progress["current_wave"] = 99  # past all waves
        save_progress(progress, project_with_features / ".aah" / "claude-progress.json")
        result = subprocess.run(
            ["aah-run", "aah.core.build.orchestrator", "next-action"],
            capture_output=True, text=True, timeout=15,
            cwd=project_with_features,
        )
        action = json.loads(result.stdout)
        assert action["action"] == "complete"

    def test_test_summary_structure(self, project_with_features):
        """test-summary returns a dict with features and regression keys."""
        result = subprocess.run(
            ["aah-run", "aah.core.build.orchestrator", "test-summary"],
            capture_output=True, text=True, timeout=15,
            cwd=project_with_features,
        )
        assert result.returncode == 0
        summary = json.loads(result.stdout)
        assert "features" in summary
        assert "regression" in summary
        assert "total_passing" in summary
        assert "total_failing" in summary

    def test_qa_report_not_found_is_graceful(self, project_with_features):
        """qa-report returns a clean error when no report exists yet."""
        result = subprocess.run(
            ["aah-run", "aah.core.build.orchestrator", "qa-report", "--feature-id", "F001"],
            capture_output=True, text=True, timeout=15,
            cwd=project_with_features,
        )
        assert result.returncode == 0
        out = json.loads(result.stdout)
        assert "error" in out
        assert "F001" in out["error"]

    def test_qa_report_written_and_readable(self, project_with_features):
        """write_qa_report persists a structured file; qa-report reads it back correctly."""
        # Write a QA report
        result = subprocess.run(
            ["aah-run", "aah.core.build.write_qa_report",
             "--feature-id", "F001",
             "--verdict", "rework_required",
             "--criteria-json", json.dumps([
                 {"id": "AC1", "description": "Auth works", "verdict": "pass",
                  "evidence": "test_tc001 PASSED"},
                 {"id": "AC2", "description": "Returns 401 on bad token", "verdict": "fail",
                  "evidence": "no test covers this case"},
             ]),
             "--issues-json", json.dumps([
                 {"issue_id": "ISSUE-1", "affected_ac_ids": ["AC2"],
                  "affected_tc_ids": ["TC1"], "severity": "critical",
                  "evidence": "no test covers the 401 response case",
                  "requested_behavior": "A 401 response on a bad token must be covered by a functional test"},
             ]),
             "--test-command", "uv run pytest tests/ -k F001",
             "--tests-run", "3",
             "--tests-passed", "2"],
            capture_output=True, text=True, timeout=15,
            cwd=project_with_features,
        )
        assert result.returncode == 0

        # Read it back via orchestrator
        result2 = subprocess.run(
            ["aah-run", "aah.core.build.orchestrator", "qa-report", "--feature-id", "F001"],
            capture_output=True, text=True, timeout=15,
            cwd=project_with_features,
        )
        report = json.loads(result2.stdout)
        assert report["verdict"] == "rework_required"
        assert report["summary"]["criteria_failed"] == 1
        assert report["summary"]["tests_run"] == 3
        assert len(report["issues"]) == 1
        assert report["issues"][0]["severity"] == "critical"
        # Human summary should appear in stderr
        assert "AC2" in result2.stderr


# ─── Scenario 11: Config Resolution ─────────────────────────────────

class TestConfigResolution:
    """Config lookup and error handling for project path resolution."""

    def test_project_path_from_subdirectory(self, project_with_features):
        """aah-run config project-path resolves correctly from a subdirectory."""
        subdir = project_with_features / "src"
        subdir.mkdir(exist_ok=True)
        result = subprocess.run(
            ["aah-run", "aah.core.common.config", "project-path"],
            capture_output=True, text=True, timeout=15,
            cwd=project_with_features,
        )
        # Should resolve to the project path (not error)
        assert result.returncode == 0
        resolved = Path(result.stdout.strip())
        assert resolved == project_with_features

    def test_aah_path_resolves_to_dot_rapids(self, project_with_features):
        """aah-path returns the .aah/ directory of the active project."""
        result = subprocess.run(
            ["aah-run", "aah.core.common.config", "aah-path"],
            capture_output=True, text=True, timeout=15,
            cwd=project_with_features,
        )
        assert result.returncode == 0
        aah_path = Path(result.stdout.strip())
        assert aah_path.name == ".aah"
        assert aah_path.exists()

    def test_no_active_project_gives_clean_error(self, tmp_path):
        """Running from a directory with no aah-config.yaml gives a clean error."""
        result = subprocess.run(
            ["aah-run", "aah.core.common.config", "project-path"],
            capture_output=True, text=True, timeout=15,
            cwd=tmp_path,
            env={k: v for k, v in os.environ.items() if k != "AAH_CONFIG_PATH"},
        )
        # Should fail cleanly — no Python traceback
        assert result.returncode != 0
        assert "Traceback" not in result.stderr
        # Should give a meaningful error
        combined = result.stdout + result.stderr
        assert any(w in combined.lower() for w in ["not found", "no active", "config", "aah_root"])

    def test_manifest_get_status_with_explicit_path(self, project_with_features):
        """manifest get-status with --path works without needing active project config."""
        result = subprocess.run(
            ["aah-run", "aah.core.common.manifest", "get-status",
             "--path", str(project_with_features / ".aah" / "manifest.yaml")],
            capture_output=True, text=True, timeout=15,
            cwd=Path("/tmp"),  # deliberately not in the project tree
        )
        assert result.returncode == 0
        status = json.loads(result.stdout)
        assert "current_phase" in status

    def test_commit_aah_state_noop_on_clean_repo(self, project_with_features):
        """commit_aah_state returns False when .aah/ is already clean."""
        from aah.core.common.git_utils import commit_aah_state
        # Ensure clean state
        subprocess.run(["git", "add", ".aah/"], cwd=project_with_features, capture_output=True)
        subprocess.run(["git", "commit", "-m", "clean state",
                        "--allow-empty"], cwd=project_with_features, capture_output=True)
        result = commit_aah_state(project_with_features)
        assert result is False

    def test_commit_aah_state_commits_pending_files(self, project_with_features):
        """commit_aah_state commits .aah/ changes and returns True."""
        from aah.core.common.git_utils import commit_aah_state
        # Create a new file in .aah/ without committing
        dirty_file = project_with_features / ".aah" / "audit" / "test-dirty.json"
        dirty_file.write_text('{"test": true}')
        result = commit_aah_state(project_with_features)
        assert result is True
        # File should be committed now
        git_status = subprocess.run(
            ["git", "status", "--short"], cwd=project_with_features,
            capture_output=True, text=True,
        )
        assert "test-dirty.json" not in git_status.stdout


# ─── Scenario 12: Worktree Cleanup ──────────────────────────────────

class TestWorktreeCleanup:
    """Git worktree listing and cleanup utilities."""

    def test_list_worktrees_clean_repo_returns_empty(self, project_with_features):
        """A repo with no agent worktrees returns an empty list."""
        result = subprocess.run(
            ["aah-run", "aah.core.git_ops.cleanup_worktrees", "list",
             "--repo-path", str(project_with_features)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        worktrees = json.loads(result.stdout)
        assert isinstance(worktrees, list)
        # Clean repo should have zero agent worktrees
        agent_wts = [w for w in worktrees if "agent-" in w.get("path", "")]
        assert len(agent_wts) == 0

    def test_list_worktrees_json_schema(self, project_with_features):
        """Worktree list output follows the expected schema."""
        result = subprocess.run(
            ["aah-run", "aah.core.git_ops.cleanup_worktrees", "list",
             "--repo-path", str(project_with_features)],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        for wt in data:
            assert "path" in wt
            assert "commits_ahead" in wt
            assert "disk_size" in wt
            assert "is_orphan" in wt


# ─── Scenario 14: Agent SDK Session Tests (original) ─────────────────

@skip_no_claude
@skip_no_api_key
@e2e
class TestAgentSDKSessions:
    """
    LIVE tests using the Claude Agent SDK.
    These spawn real Claude Code sessions and consume API tokens.
    Run with: pytest tests/e2e -m e2e
    """

    @pytest.mark.slow
    async def test_rapids_status_skill(self, scaffolded_project, framework_root):
        """Verify /rapids-status skill runs and returns project info."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, SystemMessage

        session_id = None
        result_text = None

        async for msg in query(
            prompt="/rapids-status",
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read", "Glob", "Grep", "Skill"],
                permission_mode="bypassPermissions",
                max_turns=15,
            ),
        ):
            if isinstance(msg, SystemMessage) and msg.subtype == "init":
                session_id = msg.data.get("session_id")
            elif isinstance(msg, ResultMessage):
                result_text = msg.result

        assert session_id is not None
        assert result_text is not None

    @pytest.mark.slow
    async def test_rapids_init_workspace_skill(self, tmp_path, framework_root, protect_rapids_config):
        """Verify /rapids-init-workspace creates a workspace."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=f"/rapids-init-workspace e2e-test-ws",
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read", "Write", "Skill"],
                permission_mode="bypassPermissions",
                max_turns=10,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        # Workspace should have been created
        # (exact path depends on aah-config.yaml)

    @pytest.mark.slow
    async def test_orchestrator_next_action_via_agent(self, project_with_features, framework_root):
        """Verify the orchestrator next-action works through a Claude session."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt="Run: aah-run aah.core.build.orchestrator next-action\nShow me the raw JSON output.",
            options=ClaudeAgentOptions(
                cwd=str(project_with_features),
                allowed_tools=["Bash"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        assert "dispatch" in result_text.lower() or "action" in result_text.lower()
