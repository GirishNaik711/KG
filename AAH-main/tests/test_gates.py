"""Tests for aah.core.gates modules."""

from pathlib import Path

import pytest

from aah.core.common.io_utils import write_json, write_text, write_yaml
from aah.core.common.manifest import get_default_manifest, save_manifest
from aah.core.common.progress import get_default_progress, save_progress
from aah.core.gates.validate_research_gate import validate_research_gate
from aah.core.gates.validate_analysis_gate import validate_analysis_gate
from aah.core.gates.validate_plan_gate import validate_plan_gate
from aah.core.gates.validate_feature_complete import validate_feature_complete
from aah.core.gates.validate_regression_pass import validate_regression_pass
from aah.core.common.feature_list import validate_structural_integrity
from aah.core.common.feature_utils import write_feature_frontmatter


class TestResearchGate:
    def _setup_project(self, tmp_path, phase="discuss"):
        aah_root = tmp_path / ".aah"
        aah_root.mkdir()
        (aah_root / "discuss").mkdir()
        manifest = get_default_manifest("test")
        manifest["current_phase"] = phase
        save_manifest(manifest, aah_root / "manifest.yaml")
        return tmp_path

    def test_pass_when_not_research_phase(self, tmp_path):
        proj = self._setup_project(tmp_path, phase="architecture")
        passed, issues = validate_research_gate(proj)
        assert passed

    def test_fail_when_no_activity_plan(self, tmp_path):
        proj = self._setup_project(tmp_path)
        passed, issues = validate_research_gate(proj)
        assert not passed
        assert any("activity-plan" in i.lower() for i in issues)

    def test_fail_when_activity_not_completed(self, tmp_path):
        proj = self._setup_project(tmp_path)
        aah_root = proj / ".aah"
        write_yaml({
            "activities": [
                {"activity_id": "R-SHARED-tech", "phase": "discuss", "status": "completed", "artifacts": []},
                {"activity_id": "R-SHARED-risks", "phase": "discuss", "status": "pending", "artifacts": []},
            ]
        }, aah_root / "activity-plan.yaml")
        write_text("# Tech\ncontent", aah_root / "discuss" / "tech.md")

        passed, issues = validate_research_gate(proj)
        assert not passed
        assert any("pending" in i for i in issues)

    def test_fail_when_no_research_artifacts(self, tmp_path):
        proj = self._setup_project(tmp_path)
        aah_root = proj / ".aah"
        # Empty research dir, all activities completed but no files
        write_yaml({
            "activities": [
                {"activity_id": "R-SHARED-tech", "phase": "discuss", "status": "completed", "artifacts": []},
            ]
        }, aah_root / "activity-plan.yaml")

        passed, issues = validate_research_gate(proj)
        assert not passed
        assert any("no research artifacts" in i.lower() for i in issues)

    def test_pass_when_all_completed_with_artifacts(self, tmp_path):
        proj = self._setup_project(tmp_path)
        aah_root = proj / ".aah"
        write_yaml({
            "activities": [
                {"activity_id": "R-SHARED-tech", "phase": "discuss", "status": "completed", "artifacts": []},
            ]
        }, aah_root / "activity-plan.yaml")
        write_text("# Tech\ncontent", aah_root / "discuss" / "tech-comparison.md")

        passed, issues = validate_research_gate(proj)
        assert passed

    def test_pass_when_no_research_activities_planned(self, tmp_path):
        proj = self._setup_project(tmp_path)
        aah_root = proj / ".aah"
        write_yaml({
            "activities": [
                {"activity_id": "A-SHARED-arch", "phase": "architecture", "status": "pending", "artifacts": []},
            ]
        }, aah_root / "activity-plan.yaml")

        passed, issues = validate_research_gate(proj)
        assert passed  # No research activities = gate passes


class TestAnalysisGate:
    def _setup_project(self, tmp_path, phase="architecture"):
        aah_root = tmp_path / ".aah"
        aah_root.mkdir()
        (aah_root / "architecture" / "decisions").mkdir(parents=True)
        manifest = get_default_manifest("test")
        manifest["current_phase"] = phase
        save_manifest(manifest, aah_root / "manifest.yaml")
        return tmp_path

    def test_pass_when_not_analysis_phase(self, tmp_path):
        proj = self._setup_project(tmp_path, phase="plan")
        passed, _ = validate_analysis_gate(proj)
        assert passed

    def test_fail_when_no_activity_plan(self, tmp_path):
        proj = self._setup_project(tmp_path)
        passed, issues = validate_analysis_gate(proj)
        assert not passed
        assert any("activity-plan" in i.lower() for i in issues)

    def test_fail_when_activity_not_completed(self, tmp_path):
        proj = self._setup_project(tmp_path)
        aah_root = proj / ".aah"
        write_yaml({
            "activities": [
                {"activity_id": "A-APP-L1-interaction-architecture", "phase": "architecture", "status": "pending", "artifacts": []},
            ]
        }, aah_root / "activity-plan.yaml")

        passed, issues = validate_analysis_gate(proj)
        assert not passed
        assert any("pending" in i for i in issues)

    def test_fail_when_no_adrs_for_architecture_activity(self, tmp_path):
        proj = self._setup_project(tmp_path)
        aah_root = proj / ".aah"
        write_yaml({
            "activities": [
                {"activity_id": "A-APP-L1-interaction-architecture", "phase": "architecture", "status": "completed", "artifacts": []},
            ]
        }, aah_root / "activity-plan.yaml")
        write_text("# Analysis\ncontent", aah_root / "architecture" / "arch.md")

        passed, issues = validate_analysis_gate(proj)
        assert not passed
        assert any("adr" in i.lower() for i in issues)

    def test_pass_with_completed_activities_and_adrs(self, tmp_path):
        proj = self._setup_project(tmp_path)
        aah_root = proj / ".aah"
        write_yaml({
            "activities": [
                {"activity_id": "A-APP-L1-interaction-architecture", "phase": "architecture", "status": "completed", "artifacts": []},
                {"activity_id": "A-APP-L2-framework-tooling", "phase": "architecture", "status": "completed", "artifacts": []},
            ]
        }, aah_root / "activity-plan.yaml")
        write_text("# NFR\ncontent", aah_root / "architecture" / "nfr-analysis.md")
        write_text("# ADR-001\nDecision", aah_root / "architecture" / "decisions" / "adr-001.md")

        passed, _ = validate_analysis_gate(proj)
        assert passed

    def test_pass_when_no_analysis_activities_planned(self, tmp_path):
        proj = self._setup_project(tmp_path)
        aah_root = proj / ".aah"
        write_yaml({
            "activities": [
                {"activity_id": "R-SHARED-tech", "phase": "discuss", "status": "completed", "artifacts": []},
            ]
        }, aah_root / "activity-plan.yaml")

        passed, _ = validate_analysis_gate(proj)
        assert passed


class TestPlanGate:
    def _setup_project(self, tmp_path, sample_features):
        aah_root = tmp_path / ".aah"
        for d in ["plan/features", "plan/specs", "plan/sprint-contracts"]:
            (aah_root / d).mkdir(parents=True)
        manifest = get_default_manifest("test")
        manifest["current_phase"] = "plan"
        save_manifest(manifest, aah_root / "manifest.yaml")

        # Write features
        for f in sample_features:
            contract = dict(f)
            contract.setdefault("knowledge_used", {})
            write_feature_frontmatter(contract, aah_root / "plan" / "features" / f"{f['id']}.md")

        return tmp_path

    def test_fail_when_no_features(self, tmp_path):
        aah_root = tmp_path / ".aah"
        aah_root.mkdir()
        (aah_root / "plan" / "features").mkdir(parents=True)
        manifest = get_default_manifest("test")
        manifest["current_phase"] = "plan"
        save_manifest(manifest, aah_root / "manifest.yaml")

        passed, issues = validate_plan_gate(tmp_path)
        assert not passed

    def test_fail_when_missing_dag(self, tmp_path, sample_features):
        proj = self._setup_project(tmp_path, sample_features)
        passed, issues = validate_plan_gate(proj)
        assert not passed
        assert any("dag" in i.lower() for i in issues)

    def test_fail_when_missing_waves(self, tmp_path, sample_features):
        proj = self._setup_project(tmp_path, sample_features)
        aah_root = proj / ".aah"
        # Create DAG but no waves
        from aah.core.common.dag import build_dag_from_features, dag_to_json
        G = build_dag_from_features(sample_features)
        write_json(dag_to_json(G), aah_root / "plan" / "dag.json")

        passed, issues = validate_plan_gate(proj)
        assert not passed
        assert any("waves" in i.lower() for i in issues)

    def test_plan_gate_reads_aah_markdown_features(self, tmp_path, sample_features):
        proj = self._setup_project(tmp_path, sample_features[:1])
        aah_root = proj / ".aah"
        from aah.core.common.dag import build_dag_from_features, dag_to_json
        write_json(dag_to_json(build_dag_from_features(sample_features[:1])), aah_root / "plan" / "dag.json")
        write_json({"waves": [[sample_features[0]["id"]]], "total_waves": 1}, aah_root / "plan" / "waves.json")
        write_json({"features": [{"id": sample_features[0]["id"]}]}, aah_root / "feature-list.json")
        (aah_root / "plan" / "sprint-contracts" / "wave-0-contract.md").write_text("# Wave 0\n")
        passed, issues = validate_plan_gate(proj)
        assert passed, issues


class TestFeatureComplete:
    def test_fail_no_test_results(self, tmp_path):
        aah_root = tmp_path / ".aah"
        (aah_root / "plan" / "features").mkdir(parents=True)
        (aah_root / "build" / "test-results").mkdir(parents=True)
        write_yaml({"id": "F001"}, aah_root / "plan" / "features" / "F001.yaml")

        passed, issues = validate_feature_complete(tmp_path, "F001")
        assert not passed
        assert any("test results" in i.lower() for i in issues)

    def test_fail_tests_not_passing(self, tmp_path):
        aah_root = tmp_path / ".aah"
        (aah_root / "plan" / "features").mkdir(parents=True)
        (aah_root / "build" / "test-results").mkdir(parents=True)
        write_yaml({"id": "F001"}, aah_root / "plan" / "features" / "F001.yaml")
        write_json({"passed": False, "failed_tests": [{"name": "test_login", "reason": "timeout"}]},
                   aah_root / "build" / "test-results" / "F001.json")

        passed, issues = validate_feature_complete(tmp_path, "F001")
        assert not passed

    def test_pass_tests_passing(self, tmp_path):
        aah_root = tmp_path / ".aah"
        (aah_root / "plan" / "features").mkdir(parents=True)
        (aah_root / "build" / "test-results").mkdir(parents=True)
        write_yaml({"id": "F001"}, aah_root / "plan" / "features" / "F001.yaml")
        write_json({"passed": True, "failed_tests": []},
                   aah_root / "build" / "test-results" / "F001.json")

        passed, issues = validate_feature_complete(tmp_path, "F001")
        assert passed


class TestRegressionPass:
    def test_pass_no_results_no_features(self, tmp_path):
        aah_root = tmp_path / ".aah"
        (aah_root / "build" / "test-results").mkdir(parents=True)
        passed, _ = validate_regression_pass(tmp_path)
        assert passed

    def test_fail_passing_features_but_no_regression(self, tmp_path):
        aah_root = tmp_path / ".aah"
        (aah_root / "build" / "test-results").mkdir(parents=True)
        write_json(
            {"features": [{"id": "F001", "passes": True}]},
            aah_root / "feature-list.json",
        )
        passed, issues = validate_regression_pass(tmp_path)
        assert not passed

    def test_pass_regression_passing(self, tmp_path):
        aah_root = tmp_path / ".aah"
        (aah_root / "build" / "test-results").mkdir(parents=True)
        write_json({"passed": True, "total_tests": 10, "failed_count": 0},
                   aah_root / "build" / "test-results" / "regression-latest.json")
        passed, _ = validate_regression_pass(tmp_path)
        assert passed

    def test_fail_regression_failing(self, tmp_path):
        aah_root = tmp_path / ".aah"
        (aah_root / "build" / "test-results").mkdir(parents=True)
        write_json({
            "passed": False, "total_tests": 10, "failed_count": 2,
            "failures": [{"test": "test_a", "reason": "assert"}, {"test": "test_b", "reason": "timeout"}],
        }, aah_root / "build" / "test-results" / "regression-latest.json")
        passed, issues = validate_regression_pass(tmp_path)
        assert not passed
        assert any("2" in i for i in issues)
