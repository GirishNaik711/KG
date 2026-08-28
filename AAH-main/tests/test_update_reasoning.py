"""Tests for update_reasoning script."""

import pytest
from pathlib import Path
from unittest.mock import patch

from aah.core.common.io_utils import read_yaml, write_yaml
from aah.core.build.update_reasoning import update_reasoning


@pytest.fixture
def feature_yaml(tmp_path):
    """Create a minimal feature YAML for testing."""
    # Create the project structure that update_reasoning expects
    aah_dir = tmp_path / ".aah"
    aah_dir.mkdir()
    # manifest.yaml is required for resolve_project_path to accept this dir
    (aah_dir / "manifest.yaml").write_text("project_name: test\n")
    features_dir = aah_dir / "plan" / "features"
    features_dir.mkdir(parents=True)
    yaml_path = features_dir / "F001.yaml"
    data = {
        "id": "F001",
        "spec_ref": "SPEC-001",
        "description": "Test feature",
        "dependencies": [],
        "acceptance_criteria": ["AC1"],
        "test_cases": [{"id": "TC1"}],
        "status": "pending",
        "knowledge_used": {"knowledge_folder": None},
    }
    write_yaml(data, yaml_path)
    return tmp_path  # Return project root, not yaml path


class TestInitialApproach:
    def test_creates_field(self, feature_yaml):
        """Script creates implementation_reasoning with initial_approach."""
        update_reasoning(
            feature_id="F001",
            reasoning_type="initial",
            project_path=feature_yaml,
            content="Create service following existing pattern",
            trigger=None,
            change=None,
        )

        yaml_path = feature_yaml / ".aah" / "plan" / "features" / "F001.yaml"
        result = read_yaml(yaml_path)
        assert "implementation_reasoning" in result
        assert "initial_approach" in result["implementation_reasoning"]
        assert result["implementation_reasoning"]["initial_approach"] == "Create service following existing pattern"
        assert "timestamp" in result["implementation_reasoning"]


class TestRevision:
    def test_appends_to_list(self, feature_yaml):
        """Script appends revision entries."""
        # First write initial approach
        update_reasoning(
            feature_id="F001",
            reasoning_type="initial",
            project_path=feature_yaml,
            content="Original plan",
            trigger=None,
            change=None,
        )

        # Append first revision
        update_reasoning(
            feature_id="F001",
            reasoning_type="revision",
            project_path=feature_yaml,
            content=None,
            trigger="QA failure",
            change="Fixed X",
        )

        # Append second revision
        update_reasoning(
            feature_id="F001",
            reasoning_type="revision",
            project_path=feature_yaml,
            content=None,
            trigger="Runtime error",
            change="Fixed Y",
        )

        yaml_path = feature_yaml / ".aah" / "plan" / "features" / "F001.yaml"
        result = read_yaml(yaml_path)
        assert len(result["implementation_reasoning"]["revisions"]) == 2
        assert result["implementation_reasoning"]["revisions"][0]["trigger"] == "QA failure"
        assert result["implementation_reasoning"]["revisions"][1]["trigger"] == "Runtime error"


class TestPreservesExistingFields:
    def test_preserves_existing_yaml_fields(self, feature_yaml):
        """Script does not clobber other YAML fields."""
        update_reasoning(
            feature_id="F001",
            reasoning_type="initial",
            project_path=feature_yaml,
            content="Plan",
            trigger=None,
            change=None,
        )

        yaml_path = feature_yaml / ".aah" / "plan" / "features" / "F001.yaml"
        result = read_yaml(yaml_path)
        assert result["id"] == "F001"
        assert result["spec_ref"] == "SPEC-001"
        assert result["acceptance_criteria"] == ["AC1"]
        assert result["status"] == "pending"


class TestErrorHandling:
    def test_missing_feature_yaml_exits(self, tmp_path):
        """Script exits with error if feature YAML doesn't exist."""
        # Create the .aah structure but no feature file
        (tmp_path / ".aah" / "plan" / "features").mkdir(parents=True)
        # Create a manifest so project resolves
        (tmp_path / ".aah" / "manifest.yaml").write_text("project_name: test\n")

        with pytest.raises(SystemExit):
            update_reasoning(
                feature_id="F999",
                reasoning_type="initial",
                project_path=tmp_path,
                content="Test",
                trigger=None,
                change=None,
            )
