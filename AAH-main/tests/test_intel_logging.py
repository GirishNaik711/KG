"""Tests for aah.core.intel and aah.core.logging modules."""

import json
from pathlib import Path

import pytest

from aah.core.common.io_utils import write_json, write_text, write_yaml
from aah.core.intel.check_intel_update import (
    check_intel_update_needed,
    is_significant_change,
    reset_pending_changes,
    staleness_check,
    _find_intel_dir,
    _find_intel_artifact,
    _read_pending_changes,
    _increment_pending_changes,
    THRESHOLD,
)
from aah.core.intel.build_traceability import build_traceability, render_traceability_markdown
from aah.core.logging.log_test_results import detect_test_output


# ─── is_significant_change (standalone) ────────────────────────────


class TestIsSignificantChange:
    def test_init_py(self):
        assert is_significant_change("src/__init__.py")

    def test_pyproject_toml(self):
        assert is_significant_change("pyproject.toml")

    def test_dockerfile(self):
        assert is_significant_change("Dockerfile")

    def test_docker_compose(self):
        assert is_significant_change("docker-compose.yml")

    def test_package_json(self):
        assert is_significant_change("package.json")

    def test_models_file(self):
        assert is_significant_change("src/models.py")

    def test_routes_file(self):
        assert is_significant_change("app/routes/auth.py")

    def test_api_file(self):
        assert is_significant_change("src/api/v1.py")

    def test_migration_file(self):
        assert is_significant_change("db/migrations/001_init.sql")

    def test_config_yaml(self):
        assert is_significant_change("config.yaml")

    def test_proto_extension(self):
        assert is_significant_change("protos/service.proto")

    def test_graphql_extension(self):
        assert is_significant_change("schema.graphql")

    def test_regular_python_file(self):
        assert not is_significant_change("src/utils/helpers.py")

    def test_test_file(self):
        assert not is_significant_change("tests/test_foo.py")

    def test_readme(self):
        assert not is_significant_change("README.md")

    def test_css_file(self):
        assert not is_significant_change("styles/main.css")

    def test_case_insensitive(self):
        assert is_significant_change("SRC/MODELS.PY")
        assert is_significant_change("DOCKERFILE")


# ─── _find_intel_dir / _find_intel_artifact ────────────────────────


class TestFindIntelDir:
    def test_no_rapids_dir(self, tmp_path):
        assert _find_intel_dir(tmp_path) is None

    def test_codebase_intel_dir(self, tmp_path):
        d = tmp_path / ".rapids" / "codebase-intel"
        d.mkdir(parents=True)
        assert _find_intel_dir(tmp_path) == d

    def test_brownfield_dir(self, tmp_path):
        d = tmp_path / ".rapids" / "brownfield"
        d.mkdir(parents=True)
        assert _find_intel_dir(tmp_path) == d

    def test_codebase_intel_preferred_over_brownfield(self, tmp_path):
        ci = tmp_path / ".rapids" / "codebase-intel"
        ci.mkdir(parents=True)
        bf = tmp_path / ".rapids" / "brownfield"
        bf.mkdir(parents=True)
        assert _find_intel_dir(tmp_path) == ci


class TestFindIntelArtifact:
    def test_no_intel_dir(self, tmp_path):
        assert _find_intel_artifact(tmp_path) is None

    def test_codebase_structure_md(self, tmp_path):
        d = tmp_path / ".rapids" / "brownfield"
        d.mkdir(parents=True)
        artifact = d / "codebase-structure.md"
        write_text("# Structure", artifact)
        assert _find_intel_artifact(tmp_path) == artifact

    def test_codebase_learning_md(self, tmp_path):
        d = tmp_path / ".rapids" / "codebase-intel"
        d.mkdir(parents=True)
        artifact = d / "codebase-learning.md"
        write_text("# Learnings", artifact)
        assert _find_intel_artifact(tmp_path) == artifact

    def test_codebase_profile_json(self, tmp_path):
        d = tmp_path / ".rapids" / "codebase-intel"
        d.mkdir(parents=True)
        artifact = d / "codebase-profile.json"
        write_json({"files": []}, artifact)
        assert _find_intel_artifact(tmp_path) == artifact


# ─── Pending changes counter ──────────────────────────────────────


class TestPendingChanges:
    def test_read_empty(self, tmp_path):
        data = _read_pending_changes(tmp_path)
        assert data == {"count": 0, "files": []}

    def test_increment(self, tmp_path):
        count = _increment_pending_changes(tmp_path, "src/models.py")
        assert count == 1
        data = _read_pending_changes(tmp_path)
        assert data["count"] == 1
        assert "src/models.py" in data["files"]
        assert "last_change" in data

    def test_increment_multiple(self, tmp_path):
        _increment_pending_changes(tmp_path, "src/models.py")
        _increment_pending_changes(tmp_path, "Dockerfile")
        count = _increment_pending_changes(tmp_path, "pyproject.toml")
        assert count == 3
        data = _read_pending_changes(tmp_path)
        assert data["count"] == 3
        assert len(data["files"]) == 3

    def test_no_duplicate_files(self, tmp_path):
        _increment_pending_changes(tmp_path, "src/models.py")
        _increment_pending_changes(tmp_path, "src/models.py")
        data = _read_pending_changes(tmp_path)
        assert data["count"] == 2  # count increments
        assert data["files"].count("src/models.py") == 1  # but file only listed once

    def test_file_list_capped_at_20(self, tmp_path):
        for i in range(25):
            _increment_pending_changes(tmp_path, f"file_{i}.py")
        data = _read_pending_changes(tmp_path)
        assert len(data["files"]) == 20
        assert data["count"] == 25

    def test_reset(self, tmp_path):
        _increment_pending_changes(tmp_path, "src/models.py")
        _increment_pending_changes(tmp_path, "Dockerfile")
        reset_pending_changes(tmp_path)
        data = _read_pending_changes(tmp_path)
        assert data["count"] == 0
        assert data["files"] == []
        assert "last_reset" in data


# ─── check_intel_update_needed (integration) ──────────────────────


class TestCheckIntelUpdate:
    def test_no_brownfield_dir(self, tmp_path):
        assert not check_intel_update_needed("src/main.py", tmp_path)

    def test_significant_file_change_brownfield(self, tmp_path):
        rapids = tmp_path / ".rapids" / "brownfield"
        rapids.mkdir(parents=True)
        write_text("# Structure", rapids / "codebase-structure.md")

        assert check_intel_update_needed("src/__init__.py", tmp_path)
        assert check_intel_update_needed("pyproject.toml", tmp_path)
        assert check_intel_update_needed("Dockerfile", tmp_path)

    def test_insignificant_file_change(self, tmp_path):
        rapids = tmp_path / ".rapids" / "brownfield"
        rapids.mkdir(parents=True)
        write_text("# Structure", rapids / "codebase-structure.md")

        assert not check_intel_update_needed("src/utils/helpers.py", tmp_path)
        assert not check_intel_update_needed("tests/test_foo.py", tmp_path)

    def test_significant_file_change_greenfield(self, tmp_path):
        """Greenfield projects with codebase-intel/ artifacts also trigger."""
        rapids = tmp_path / ".rapids" / "codebase-intel"
        rapids.mkdir(parents=True)
        write_json({"files": []}, rapids / "codebase-profile.json")

        assert check_intel_update_needed("src/__init__.py", tmp_path)
        assert check_intel_update_needed("src/api/routes.py", tmp_path)

    def test_no_intel_artifacts_no_trigger(self, tmp_path):
        """Empty intel dir without actual artifacts should not trigger."""
        rapids = tmp_path / ".rapids" / "codebase-intel"
        rapids.mkdir(parents=True)
        # Dir exists but no recognized artifacts
        assert not check_intel_update_needed("pyproject.toml", tmp_path)


# ─── staleness_check ──────────────────────────────────────────────


class TestStalenessCheck:
    def test_no_intel(self, tmp_path):
        result = staleness_check(tmp_path)
        assert result["has_intel"] is False
        assert result["stale"] is False
        assert result["intel_path"] is None
        assert "No codebase intelligence found" in result["recommendation"]

    def test_fresh_brownfield(self, tmp_path):
        rapids = tmp_path / ".rapids" / "brownfield"
        rapids.mkdir(parents=True)
        write_text("# Structure", rapids / "codebase-structure.md")

        result = staleness_check(tmp_path)
        assert result["has_intel"] is True
        assert result["stale"] is False
        assert result["pending_changes"] == 0

    def test_stale_by_pending_changes(self, tmp_path):
        # Use codebase-intel dir since _increment_pending_changes creates it,
        # which would shadow the brownfield dir in _find_intel_dir.
        rapids = tmp_path / ".rapids" / "codebase-intel"
        rapids.mkdir(parents=True)
        write_text("# Structure", rapids / "codebase-structure.md")

        # Accumulate enough pending changes to cross threshold
        for i in range(THRESHOLD):
            _increment_pending_changes(tmp_path, f"file_{i}.py")

        result = staleness_check(tmp_path)
        assert result["has_intel"] is True
        assert result["stale"] is True
        assert result["pending_changes"] >= THRESHOLD
        assert "stale" in result["recommendation"].lower()

    def test_greenfield_staleness(self, tmp_path):
        """Greenfield projects use codebase-intel/ dir."""
        rapids = tmp_path / ".rapids" / "codebase-intel"
        rapids.mkdir(parents=True)
        write_json({"files": []}, rapids / "codebase-profile.json")

        result = staleness_check(tmp_path)
        assert result["has_intel"] is True
        assert result["stale"] is False
        assert "codebase-intel" in result["intel_path"]


class TestBuildTraceability:
    def _setup_project(self, tmp_path):
        rapids = tmp_path / ".rapids"
        for d in ["plan/features", "plan/specs", "research", "analysis/decisions",
                   "implement/test-results", "audit"]:
            (rapids / d).mkdir(parents=True)
        return tmp_path

    def test_empty_project(self, tmp_path):
        proj = self._setup_project(tmp_path)
        matrix = build_traceability(proj)
        assert matrix["specs"] == {}
        assert matrix["features"] == {}

    def test_with_features_and_specs(self, tmp_path):
        proj = self._setup_project(tmp_path)
        rapids = proj / ".rapids"

        # Create spec
        write_text("# Spec 001\nOverview", rapids / "plan" / "specs" / "SPEC-001.md")

        # Create features
        write_yaml({
            "id": "F001", "spec_ref": "SPEC-001", "description": "Auth",
            "dependencies": [], "acceptance_criteria": ["AC1", "AC2"],
            "test_cases": [{"id": "TC1"}, {"id": "TC2"}], "status": "passed",
        }, rapids / "plan" / "features" / "F001.yaml")

        # Create test results
        write_json({"passed": True}, rapids / "implement" / "test-results" / "F001.json")

        matrix = build_traceability(proj)
        assert "SPEC-001" in matrix["specs"]
        assert "F001" in matrix["features"]
        assert matrix["features"]["F001"]["has_test_results"] is True
        assert matrix["features"]["F001"]["acceptance_criteria_count"] == 2
        assert matrix["features"]["F001"]["test_cases_count"] == 2
        assert "F001" in matrix["specs"]["SPEC-001"]["features"]

    def test_orphaned_feature(self, tmp_path):
        proj = self._setup_project(tmp_path)
        rapids = proj / ".rapids"

        write_yaml({
            "id": "F001", "spec_ref": "MISSING-SPEC", "description": "Auth",
            "dependencies": [], "acceptance_criteria": ["AC1"],
            "test_cases": [{"id": "TC1"}], "status": "pending",
        }, rapids / "plan" / "features" / "F001.yaml")

        matrix = build_traceability(proj)
        assert len(matrix["orphaned_features"]) == 1
        assert matrix["orphaned_features"][0]["missing_spec"] == "MISSING-SPEC"

    def test_render_markdown(self, tmp_path):
        proj = self._setup_project(tmp_path)
        rapids = proj / ".rapids"

        write_text("# Spec", rapids / "plan" / "specs" / "S1.md")
        write_yaml({
            "id": "F001", "spec_ref": "S1", "description": "Feature",
            "dependencies": [], "acceptance_criteria": ["AC"],
            "test_cases": [{"id": "TC"}], "status": "pending",
        }, rapids / "plan" / "features" / "F001.yaml")

        matrix = build_traceability(proj)
        md = render_traceability_markdown(matrix)
        assert "# Traceability Matrix" in md
        assert "F001" in md
        assert "S1" in md


class TestDetectTestOutput:
    def test_pytest_output(self):
        stdout = "====== 5 passed, 2 failed in 1.23s ======"
        result = detect_test_output(stdout, "", "pytest tests/")
        assert result is not None
        assert result["passed"] == 5
        assert result["failed"] == 2
        assert result["framework"] == "pytest"
        assert not result["success"]

    def test_pytest_all_pass(self):
        stdout = "====== 10 passed in 0.5s ======"
        result = detect_test_output(stdout, "", "python -m pytest")
        assert result is not None
        assert result["passed"] == 10
        assert result["failed"] == 0
        assert result["success"]

    def test_jest_output(self):
        stdout = "Tests:  2 failed, 8 passed, 10 total"
        result = detect_test_output(stdout, "", "npm test")
        assert result is not None
        assert result["passed"] == 8
        assert result["failed"] == 2
        assert result["framework"] == "jest"

    def test_non_test_command(self):
        result = detect_test_output("output", "", "ls -la")
        assert result is None

    def test_test_command_no_results(self):
        result = detect_test_output("compiling...", "", "cargo test")
        # Returns None since no recognizable output pattern
        assert result is None
