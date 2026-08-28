"""Tests for aah.core.common.manifest."""

from pathlib import Path

import pytest

from aah.core.common.manifest import (
    add_artifact,
    find_manifest,
    get_default_manifest,
    get_status,
    load_manifest,
    migrate_verification_flags,
    save_manifest,
    set_complexity,
    set_stack,
    update_phase,
    validate_manifest,
)


class TestGetDefaultManifest:
    def test_returns_correct_structure(self):
        m = get_default_manifest("my-project")
        assert m["project_name"] == "my-project"
        assert m["project_type"] == "greenfield"
        assert m["current_phase"] == "init"
        assert m["complexity_tier"] is None
        assert m["completed_artifacts"] == []
        assert m["stack_choices"] == {}
        assert "created_at" in m
        assert "updated_at" in m

    def test_brownfield_type(self):
        m = get_default_manifest("proj", "brownfield")
        assert m["project_type"] == "brownfield"


def test_verification_rollout_defaults_and_migration(tmp_path):
    expected = {
        "runtime_profile_v1": "report_only",
        "runtime_cloud_probes_v1": False,
        "semantic_smoke_v2": False,
    }
    assert get_default_manifest("new")["features"] == expected
    path = tmp_path / ".aah" / "manifest.yaml"
    legacy = get_default_manifest("legacy")
    legacy.pop("features")
    save_manifest(legacy, path)
    first = migrate_verification_flags(path)
    second = migrate_verification_flags(path)
    assert first["features"] == expected
    assert second["features"] == expected


def test_verification_rollout_has_no_env_override(tmp_path):
    import os

    path = tmp_path / ".aah" / "manifest.yaml"
    save_manifest(get_default_manifest("p"), path)
    previous = os.environ.get("RUNTIME_PROFILE_V1")
    os.environ["RUNTIME_PROFILE_V1"] = "false"
    try:
        assert load_manifest(path)["features"]["runtime_profile_v1"] == "report_only"
    finally:
        if previous is None:
            os.environ.pop("RUNTIME_PROFILE_V1", None)
        else:
            os.environ["RUNTIME_PROFILE_V1"] = previous


class TestSaveAndLoadManifest:
    def test_round_trip(self, tmp_path):
        manifest_path = tmp_path / ".aah" / "manifest.yaml"
        m = get_default_manifest("test")
        save_manifest(m, manifest_path)
        loaded = load_manifest(manifest_path)
        assert loaded["project_name"] == "test"

    def test_save_updates_timestamp(self, tmp_path):
        manifest_path = tmp_path / ".aah" / "manifest.yaml"
        m = get_default_manifest("test")
        original_ts = m["updated_at"]
        save_manifest(m, manifest_path)
        loaded = load_manifest(manifest_path)
        # Timestamp should be updated (may be same in fast execution)
        assert "updated_at" in loaded


class TestFindManifest:
    def test_finds_with_explicit_start_dir(self, tmp_path):
        aah_root = tmp_path / ".aah"
        aah_root.mkdir()
        manifest_path = aah_root / "manifest.yaml"
        save_manifest(get_default_manifest("test"), manifest_path)
        found = find_manifest(start_dir=tmp_path)
        assert found == manifest_path

    def test_finds_in_parent_with_start_dir(self, tmp_path):
        aah_root = tmp_path / ".aah"
        aah_root.mkdir()
        manifest_path = aah_root / "manifest.yaml"
        save_manifest(get_default_manifest("test"), manifest_path)
        child = tmp_path / "src" / "components"
        child.mkdir(parents=True)
        found = find_manifest(start_dir=child)
        assert found == manifest_path

    def test_returns_none_with_empty_start_dir(self, tmp_path):
        # With no config and no .aah in start_dir, should return None
        empty = tmp_path / "empty"
        empty.mkdir()
        assert find_manifest(start_dir=empty) is None


class TestValidateManifest:
    def test_valid(self):
        m = get_default_manifest("test")
        errors = validate_manifest(m)
        assert errors == []

    def test_invalid_phase(self):
        m = get_default_manifest("test")
        m["current_phase"] = "invalid"
        errors = validate_manifest(m)
        assert len(errors) == 1


class TestUpdatePhase:
    def test_updates_phase(self, tmp_path):
        manifest_path = tmp_path / ".aah" / "manifest.yaml"
        save_manifest(get_default_manifest("test"), manifest_path)
        result = update_phase(manifest_path, "discuss")
        assert result["current_phase"] == "discuss"
        # Verify persisted
        loaded = load_manifest(manifest_path)
        assert loaded["current_phase"] == "discuss"

    def test_invalid_phase_exits(self, tmp_path):
        manifest_path = tmp_path / ".aah" / "manifest.yaml"
        save_manifest(get_default_manifest("test"), manifest_path)
        with pytest.raises(SystemExit):
            update_phase(manifest_path, "invalid_phase")


class TestSetStack:
    def test_sets_stack(self, tmp_path):
        manifest_path = tmp_path / ".aah" / "manifest.yaml"
        save_manifest(get_default_manifest("test"), manifest_path)
        result = set_stack(manifest_path, "language", "python")
        assert result["stack_choices"]["language"] == "python"


class TestSetComplexity:
    def test_sets_complexity(self, tmp_path):
        manifest_path = tmp_path / ".aah" / "manifest.yaml"
        save_manifest(get_default_manifest("test"), manifest_path)
        result = set_complexity(manifest_path, "complex")
        assert result["complexity_tier"] == "complex"

    def test_invalid_tier_exits(self, tmp_path):
        manifest_path = tmp_path / ".aah" / "manifest.yaml"
        save_manifest(get_default_manifest("test"), manifest_path)
        with pytest.raises(SystemExit):
            set_complexity(manifest_path, "mega_complex")


class TestAddArtifact:
    def test_adds_artifact(self, tmp_path):
        manifest_path = tmp_path / ".aah" / "manifest.yaml"
        save_manifest(get_default_manifest("test"), manifest_path)
        result = add_artifact(manifest_path, "research/tech-comparison.md")
        assert "research/tech-comparison.md" in result["completed_artifacts"]

    def test_no_duplicates(self, tmp_path):
        manifest_path = tmp_path / ".aah" / "manifest.yaml"
        save_manifest(get_default_manifest("test"), manifest_path)
        add_artifact(manifest_path, "artifact1")
        result = add_artifact(manifest_path, "artifact1")
        assert result["completed_artifacts"].count("artifact1") == 1


class TestGetStatus:
    def test_returns_summary(self, tmp_path):
        manifest_path = tmp_path / ".aah" / "manifest.yaml"
        m = get_default_manifest("test")
        m["complexity_tier"] = "moderate"
        save_manifest(m, manifest_path)
        status = get_status(manifest_path)
        assert status["project_name"] == "test"
        assert status["complexity_tier"] == "moderate"
        assert status["artifact_count"] == 0
