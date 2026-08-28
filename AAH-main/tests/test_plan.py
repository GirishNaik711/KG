"""Tests for aah.core.plan modules."""

import subprocess
import sys
from pathlib import Path

import pytest

from aah.core.common.io_utils import read_json, read_yaml, write_json, write_yaml
from aah.core.common.feature_utils import write_feature_frontmatter
from aah.core.plan.build_feature_list import build_feature_list
from aah.core.plan.build_dag import build_and_validate_dag
from aah.core.plan.compute_waves import main as compute_waves_main


class TestBuildFeatureList:
    def _setup_rapids_structure(self, tmp_path, sample_features):
        """Create .rapids/plan/features/ structure matching real layout."""
        rapids_dir = tmp_path / ".rapids"
        plan_dir = rapids_dir / "plan"
        features_dir = plan_dir / "features"
        features_dir.mkdir(parents=True)
        # Write greenfield manifest (no cumulative iteration logic)
        write_yaml({"project_type": "greenfield"}, rapids_dir / "manifest.yaml")
        for f in sample_features:
            contract = dict(f)
            contract.setdefault("knowledge_used", {})
            write_feature_frontmatter(contract, features_dir / f"{f['id']}.md")
        output_path = rapids_dir / "feature-list.json"
        return features_dir, output_path

    def test_builds_from_yamls(self, tmp_path, sample_features):
        features_dir, output_path = self._setup_rapids_structure(tmp_path, sample_features)

        result = build_feature_list(features_dir, output_path)
        assert "features" in result
        assert len(result["features"]) == 5
        # All should have passes: False
        for f in result["features"]:
            assert f["passes"] is False
            assert "id" in f
            assert "description" in f
            assert "dependencies" in f

    def test_empty_dir_exits(self, tmp_path):
        rapids_dir = tmp_path / ".rapids" / "plan" / "features"
        rapids_dir.mkdir(parents=True)
        write_yaml({"project_type": "greenfield"}, tmp_path / ".rapids" / "manifest.yaml")
        output_path = tmp_path / ".rapids" / "feature-list.json"
        with pytest.raises(SystemExit):
            build_feature_list(rapids_dir, output_path)

    def test_duplicate_ids_exits(self, tmp_path):
        rapids_dir = tmp_path / ".rapids"
        features_dir = rapids_dir / "plan" / "features"
        features_dir.mkdir(parents=True)
        write_yaml({"project_type": "greenfield"}, rapids_dir / "manifest.yaml")
        output_path = rapids_dir / "feature-list.json"
        feature = {
            "id": "F001", "spec_ref": "S1", "description": "Dup",
            "dependencies": [], "acceptance_criteria": ["AC"],
            "test_cases": [{"id": "TC1"}], "status": "pending",
        }
        write_yaml(feature, features_dir / "F001.yaml")
        write_yaml(feature, features_dir / "F001_copy.yaml")
        with pytest.raises(SystemExit):
            build_feature_list(features_dir, output_path)

    def test_invalid_feature_exits(self, tmp_path):
        rapids_dir = tmp_path / ".rapids"
        features_dir = rapids_dir / "plan" / "features"
        features_dir.mkdir(parents=True)
        write_yaml({"project_type": "greenfield"}, rapids_dir / "manifest.yaml")
        output_path = rapids_dir / "feature-list.json"
        # Missing required fields
        write_yaml({"id": "F001"}, features_dir / "F001.yaml")
        with pytest.raises(SystemExit):
            build_feature_list(features_dir, output_path)


class TestBuildAndValidateDag:
    def test_valid_dag(self, tmp_path, sample_features):
        features_dir = tmp_path / "features"
        features_dir.mkdir()
        for f in sample_features:
            contract = dict(f)
            contract.setdefault("knowledge_used", {})
            write_feature_frontmatter(contract, features_dir / f"{f['id']}.md")

        dag_data = build_and_validate_dag(features_dir)
        assert dag_data["stats"]["is_dag"] is True
        assert dag_data["stats"]["total_nodes"] == 5

    def test_cyclic_exits(self, tmp_path):
        features_dir = tmp_path / "features"
        features_dir.mkdir()
        write_feature_frontmatter({
            "id": "A", "spec_ref": "S", "description": "A", "dependencies": ["B"],
            "acceptance_criteria": ["AC"], "test_cases": [{"id": "T"}], "status": "pending",
        }, features_dir / "A.md")
        write_feature_frontmatter({
            "id": "B", "spec_ref": "S", "description": "B", "dependencies": ["A"],
            "acceptance_criteria": ["AC"], "test_cases": [{"id": "T"}], "status": "pending",
        }, features_dir / "B.md")
        with pytest.raises(SystemExit):
            build_and_validate_dag(features_dir)

    def test_build_dag_validates_markdown_spec_refs(self, tmp_path):
        features_dir = tmp_path / ".aah" / "plan" / "features"
        specs_dir = tmp_path / ".aah" / "plan" / "specs"
        features_dir.mkdir(parents=True)
        specs_dir.mkdir(parents=True)
        (specs_dir / "SPEC-001-leaf.md").write_text("# Leaf\n")
        write_feature_frontmatter(
            {"id": "F001", "spec_ref": "SPEC-001", "description": "Leaf",
             "dependencies": [], "acceptance_criteria": [], "test_cases": [],
             "status": "pending", "knowledge_used": {}},
            features_dir / "F001.md",
        )
        result = build_and_validate_dag(features_dir)
        assert result["stats"]["total_nodes"] == 1

    def test_build_dag_rejects_dangling_markdown_spec_ref(self, tmp_path):
        features_dir = tmp_path / ".aah" / "plan" / "features"
        (tmp_path / ".aah" / "plan" / "specs").mkdir(parents=True)
        features_dir.mkdir(parents=True)
        write_feature_frontmatter(
            {"id": "F001", "spec_ref": "SPEC-MISSING", "description": "Leaf",
             "dependencies": [], "acceptance_criteria": [], "test_cases": [],
             "status": "pending", "knowledge_used": {}},
            features_dir / "F001.md",
        )
        with pytest.raises(SystemExit):
            build_and_validate_dag(features_dir)


def _compute_waves_project(tmp_path, stack_choices=None):
    aah = tmp_path / ".aah"
    feature = {"id": "F001", "title": "F001", "module_ref": "MOD-TEST",
               "spec_ref": "SPEC-001", "description": "Leaf", "layers": ["backend"],
               "dependencies": [], "file_scope": ["src/leaf.py"],
               "acceptance_criteria": [], "test_cases": [], "status": "pending",
               "knowledge_used": {}}
    (aah / "plan" / "features").mkdir(parents=True)
    write_feature_frontmatter(feature, aah / "plan" / "features" / "F001.md")
    from aah.core.common.dag import build_dag_from_features, dag_to_json
    write_json(dag_to_json(build_dag_from_features([feature])), aah / "plan" / "dag.json")
    write_yaml({"project_name": "p", "project_type": "greenfield", "current_phase": "plan",
                "complexity_tier": "trivial", "stack_choices": {} if stack_choices is None else stack_choices,
                "features": {}}, aah / "manifest.yaml")
    return aah


def test_compute_waves_autogenerates_aah_checkpoint_config(tmp_path):
    aah = _compute_waves_project(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "aah.core.plan.compute_waves",
         "--dag-path", str(aah / "plan" / "dag.json"),
         "--output", str(aah / "plan" / "waves.json")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (aah / "plan" / "checkpoint-config.yaml").exists()




def test_compute_waves_active_profile_cannot_be_skipped(tmp_path):
    aah = _compute_waves_project(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "aah.core.plan.compute_waves",
         "--dag-path", str(aah / "plan" / "dag.json"),
         "--output", str(aah / "plan" / "waves.json"), "--skip-checkpoints"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (aah / "plan" / "checkpoint-config.yaml").exists()
    assert not (aah / "plan" / "smoke-tests").exists()

@pytest.mark.parametrize("contract_state", ["missing", "malformed", "empty"])
def test_compute_waves_requires_current_markdown_contract(tmp_path, contract_state):
    aah = _compute_waves_project(tmp_path)
    contract = aah / "plan" / "features" / "F001.md"
    if contract_state == "missing":
        contract.unlink()
    elif contract_state == "malformed":
        contract.write_text("# no frontmatter\n")
    else:
        contract.write_text("---\n{}\n---\n")
    write_yaml({"stale": True}, aah / "plan" / "checkpoint-config.yaml")
    result = subprocess.run(
        [sys.executable, "-m", "aah.core.plan.compute_waves",
         "--dag-path", str(aah / "plan" / "dag.json"),
         "--output", str(aah / "plan" / "waves.json")],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert not (aah / "plan" / "checkpoint-config.yaml").exists()


def test_compute_waves_rejects_id_only_schema_invalid_markdown_contract(
    tmp_path,
):
    aah = _compute_waves_project(tmp_path)
    contract = aah / "plan" / "features" / "F001.md"
    write_feature_frontmatter({"id": "F001"}, contract)
    write_yaml({"stale": True}, aah / "plan" / "checkpoint-config.yaml")

    result = subprocess.run(
        [sys.executable, "-m", "aah.core.plan.compute_waves",
         "--dag-path", str(aah / "plan" / "dag.json"),
         "--output", str(aah / "plan" / "waves.json")],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "verification profile generation failed" in result.stderr
    assert "verification_profiles" not in result.stdout
    assert not (aah / "plan" / "checkpoint-config.yaml").exists()


def _compute_waves_project_with_endpoint(tmp_path, endpoints):
    """A compute-waves project whose single feature declares HTTP endpoints."""
    aah = tmp_path / ".aah"
    feature = {
        "id": "F001", "title": "F001", "module_ref": "MOD-TEST",
        "spec_ref": "SPEC-001", "description": "Leaf", "layers": ["backend"],
        "dependencies": [], "file_scope": ["src/leaf.py"],
        "acceptance_criteria": [], "test_cases": [], "status": "pending",
        "knowledge_used": {}, "endpoints": endpoints,
    }
    (aah / "plan" / "features").mkdir(parents=True)
    write_feature_frontmatter(feature, aah / "plan" / "features" / "F001.md")
    from aah.core.common.dag import build_dag_from_features, dag_to_json
    write_json(dag_to_json(build_dag_from_features([feature])), aah / "plan" / "dag.json")
    write_yaml({"project_name": "p", "project_type": "greenfield", "current_phase": "plan",
                "complexity_tier": "trivial", "stack_choices": {},
                "features": {}}, aah / "manifest.yaml")
    return aah


def test_compute_waves_writes_smoke_to_aah(tmp_path):
    """compute_waves writes schema-v2 smoke YAML under .aah (not .rapids)."""
    aah = _compute_waves_project_with_endpoint(
        tmp_path,
        endpoints=[
            {"method": "GET", "path": "/health"},
            {"method": "GET", "path": "/orders",
             "assertions": [{"type": "status_in", "values": [200]}]},
        ],
    )
    result = subprocess.run(
        [sys.executable, "-m", "aah.core.plan.compute_waves",
         "--dag-path", str(aah / "plan" / "dag.json"),
         "--output", str(aah / "plan" / "waves.json")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    smoke_path = aah / "plan" / "smoke-tests" / "wave-0.yaml"
    assert smoke_path.exists()
    smoke_def = read_yaml(smoke_path)
    assert smoke_def["schema_version"] == 2
    assert {
        s["request"]["path"] for s in smoke_def["steps"] if s.get("request")
    } == {"/health", "/orders"}
    # No stale .rapids smoke output.
    assert not (tmp_path / ".rapids" / "plan" / "smoke-tests").exists()


def test_compute_waves_smoke_failure_invalidates_stale(tmp_path):
    """A non-health endpoint with no assertion fails smoke and invalidates stale YAML."""
    aah = _compute_waves_project_with_endpoint(
        tmp_path,
        endpoints=[{"method": "GET", "path": "/orders", "assertions": []}],
    )
    # Seed a stale smoke file that must be removed on failure.
    smoke_dir = aah / "plan" / "smoke-tests"
    smoke_dir.mkdir(parents=True)
    (smoke_dir / "wave-0.yaml").write_text("stale: true\n")
    result = subprocess.run(
        [sys.executable, "-m", "aah.core.plan.compute_waves",
         "--dag-path", str(aah / "plan" / "dag.json"),
         "--output", str(aah / "plan" / "waves.json")],
        capture_output=True, text=True,
    )
    # compute_waves itself still exits 0 (smoke failure is a warning), but the
    # stale wave file is invalidated and the error type is sanitized.
    assert result.returncode == 0, result.stderr
    assert "smoke test generation failed" in result.stderr
    assert not (smoke_dir / "wave-0.yaml").exists()
