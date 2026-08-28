"""Tests for aah.core.common.feature_list."""

from pathlib import Path

import pytest
import yaml

from aah.core.common.feature_list import (
    get_feature_by_id,
    get_passing_features,
    get_pending_features,
    get_progress_summary,
    load_feature_list,
    save_feature_list,
    sync_features_from_yaml,
    update_feature_status,
    validate_json_matches_yamls,
    validate_structural_integrity,
    validate_sync_integrity,
)


class TestLoadAndSave:
    def test_round_trip(self, tmp_path, sample_feature_list):
        path = tmp_path / "feature-list.json"
        save_feature_list(sample_feature_list, path)
        loaded = load_feature_list(path)
        assert len(loaded["features"]) == len(sample_feature_list["features"])

    def test_load_nonexistent(self, tmp_path):
        result = load_feature_list(tmp_path / "nonexistent.json")
        assert result == {"features": []}


class TestGetFeatureById:
    def test_found(self, sample_feature_list):
        f = get_feature_by_id(sample_feature_list, "F001")
        assert f is not None
        assert f["id"] == "F001"

    def test_not_found(self, sample_feature_list):
        assert get_feature_by_id(sample_feature_list, "F999") is None


class TestUpdateFeatureStatus:
    def test_marks_passing(self, tmp_path, sample_feature_list):
        path = tmp_path / "feature-list.json"
        save_feature_list(sample_feature_list, path)
        result = update_feature_status(path, "F001", True, skip_test_check=True)
        f = get_feature_by_id(result, "F001")
        assert f["passes"] is True

    def test_marks_failing(self, tmp_path, sample_feature_list):
        path = tmp_path / "feature-list.json"
        # First set to passing
        for f in sample_feature_list["features"]:
            f["passes"] = True
        save_feature_list(sample_feature_list, path)
        result = update_feature_status(path, "F002", False)
        f = get_feature_by_id(result, "F002")
        assert f["passes"] is False

    def test_unknown_feature_exits(self, tmp_path, sample_feature_list):
        path = tmp_path / "feature-list.json"
        save_feature_list(sample_feature_list, path)
        with pytest.raises(SystemExit):
            update_feature_status(path, "F999", True)


class TestUpdateFeatureStatusSyncsProgress:
    """Tests that update_feature_status syncs to claude-progress.json."""

    def test_updates_progress_on_pass(self, tmp_path, sample_feature_list):
        """passes=True sets last_completed_feature and removes from in_progress."""
        from aah.core.common.progress import get_default_progress, save_progress
        from aah.core.common.io_utils import read_json

        fl_path = tmp_path / "feature-list.json"
        save_feature_list(sample_feature_list, fl_path)

        # Create a progress file with F001 in_progress
        progress = get_default_progress()
        progress["in_progress_features"] = ["F001", "F002"]
        save_progress(progress, tmp_path / "claude-progress.json")

        update_feature_status(fl_path, "F001", True, skip_test_check=True)

        updated = read_json(tmp_path / "claude-progress.json")
        assert updated["last_completed_feature"] == "F001"
        assert "F001" not in updated["in_progress_features"]
        assert "F002" in updated["in_progress_features"]

    def test_does_not_update_progress_on_fail(self, tmp_path, sample_feature_list):
        """passes=False leaves progress unchanged."""
        from aah.core.common.progress import get_default_progress, save_progress
        from aah.core.common.io_utils import read_json

        fl_path = tmp_path / "feature-list.json"
        save_feature_list(sample_feature_list, fl_path)

        progress = get_default_progress()
        progress["last_completed_feature"] = None
        progress["in_progress_features"] = ["F001"]
        save_progress(progress, tmp_path / "claude-progress.json")

        update_feature_status(fl_path, "F001", False)

        updated = read_json(tmp_path / "claude-progress.json")
        assert updated["last_completed_feature"] is None
        assert updated["in_progress_features"] == ["F001"]

    def test_tolerates_missing_progress_file(self, tmp_path, sample_feature_list):
        """No crash when claude-progress.json doesn't exist."""
        fl_path = tmp_path / "feature-list.json"
        save_feature_list(sample_feature_list, fl_path)

        # No progress file — should not crash
        result = update_feature_status(fl_path, "F001", True, skip_test_check=True)
        assert get_feature_by_id(result, "F001")["passes"] is True
        # Progress file should still not exist (we don't create it)
        assert not (tmp_path / "claude-progress.json").exists()


class TestFilterFeatures:
    def test_pending(self, sample_feature_list):
        pending = get_pending_features(sample_feature_list)
        assert len(pending) == 5  # All are pending (passes=False)

    def test_passing(self, sample_feature_list):
        sample_feature_list["features"][0]["passes"] = True
        sample_feature_list["features"][1]["passes"] = True
        passing = get_passing_features(sample_feature_list)
        assert len(passing) == 2

    def test_pending_after_some_pass(self, sample_feature_list):
        sample_feature_list["features"][0]["passes"] = True
        pending = get_pending_features(sample_feature_list)
        assert len(pending) == 4


class TestValidateStructuralIntegrity:
    def test_no_changes_valid(self, sample_feature_list):
        import copy
        proposed = copy.deepcopy(sample_feature_list)
        errors = validate_structural_integrity(sample_feature_list, proposed)
        assert errors == []

    def test_status_change_valid(self, sample_feature_list):
        import copy
        proposed = copy.deepcopy(sample_feature_list)
        proposed["features"][0]["passes"] = True
        proposed["features"][2]["passes"] = True
        errors = validate_structural_integrity(sample_feature_list, proposed)
        assert errors == []

    def test_feature_removed_invalid(self, sample_feature_list):
        import copy
        proposed = copy.deepcopy(sample_feature_list)
        proposed["features"].pop()
        errors = validate_structural_integrity(sample_feature_list, proposed)
        assert len(errors) > 0
        assert any("removed" in e.lower() for e in errors)

    def test_feature_added_allowed(self, sample_feature_list):
        """Feature addition is allowed — new YAML files create new features."""
        import copy
        proposed = copy.deepcopy(sample_feature_list)
        proposed["features"].append({"id": "F999", "passes": False, "description": "New", "spec_ref": "S", "dependencies": []})
        errors = validate_structural_integrity(sample_feature_list, proposed)
        assert errors == []

    def test_description_changed_allowed(self, sample_feature_list):
        """Field changes are allowed — they come from legitimate YAML sync."""
        import copy
        proposed = copy.deepcopy(sample_feature_list)
        proposed["features"][0]["description"] = "MODIFIED"
        errors = validate_structural_integrity(sample_feature_list, proposed)
        assert errors == []

    def test_id_changed_is_removal(self, sample_feature_list):
        """Changing an ID is effectively removing the old feature."""
        import copy
        proposed = copy.deepcopy(sample_feature_list)
        proposed["features"][0]["id"] = "F999"
        errors = validate_structural_integrity(sample_feature_list, proposed)
        assert len(errors) > 0
        assert any("removed" in e.lower() for e in errors)


class TestProgressSummary:
    def test_all_pending(self, sample_feature_list):
        summary = get_progress_summary(sample_feature_list)
        assert summary["total"] == 5
        assert summary["passing"] == 0
        assert summary["failing"] == 5
        assert summary["completion_pct"] == 0.0

    def test_some_passing(self, sample_feature_list):
        sample_feature_list["features"][0]["passes"] = True
        sample_feature_list["features"][1]["passes"] = True
        summary = get_progress_summary(sample_feature_list)
        assert summary["passing"] == 2
        assert summary["completion_pct"] == 40.0

    def test_empty_list(self):
        summary = get_progress_summary({"features": []})
        assert summary["total"] == 0
        assert summary["completion_pct"] == 0.0


# --- Helper to create feature YAML files ---

def _write_feature_yaml(features_dir: Path, feature_id: str, data: dict) -> Path:
    """Write a feature YAML file and return its path."""
    path = features_dir / f"{feature_id}.yaml"
    with open(path, "w") as f:
        yaml.dump(data, f)
    return path


def _make_features_dir(tmp_path: Path, features: list[dict]) -> Path:
    """Create a features directory with YAML files for each feature."""
    features_dir = tmp_path / "plan" / "features"
    features_dir.mkdir(parents=True)
    for feat in features:
        _write_feature_yaml(features_dir, feat["id"], feat)
    return features_dir


class TestSyncFeaturesFromYaml:
    """Tests for sync_features_from_yaml."""

    def test_creates_json_from_yamls(self, tmp_path):
        """Creates feature-list.json when it doesn't exist."""
        features = [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1"},
            {"id": "F002", "description": "Reg", "dependencies": [], "spec_ref": "S1"},
        ]
        features_dir = _make_features_dir(tmp_path, features)
        fl_path = tmp_path / "feature-list.json"

        result = sync_features_from_yaml(features_dir, fl_path)

        assert fl_path.exists()
        assert len(result["features"]) == 2
        assert result["features"][0]["id"] == "F001"
        assert result["features"][1]["id"] == "F002"
        # All new features get passes=False
        assert all(f["passes"] is False for f in result["features"])

    def test_preserves_passes_on_sync(self, tmp_path):
        """Existing passes=True is preserved when syncing updated YAML."""
        features = [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1"},
            {"id": "F002", "description": "Reg", "dependencies": [], "spec_ref": "S1"},
        ]
        features_dir = _make_features_dir(tmp_path, features)
        fl_path = tmp_path / "feature-list.json"

        # Create initial JSON with F001 passing
        from aah.core.common.io_utils import write_json
        write_json({"features": [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1", "passes": True},
            {"id": "F002", "description": "Reg", "dependencies": [], "spec_ref": "S1", "passes": False},
        ]}, fl_path)

        # Update YAML with new description
        _write_feature_yaml(features_dir, "F001", {
            "id": "F001", "description": "Auth v2", "dependencies": [], "spec_ref": "S1"
        })

        result = sync_features_from_yaml(features_dir, fl_path)

        f001 = next(f for f in result["features"] if f["id"] == "F001")
        assert f001["passes"] is True  # Preserved
        assert f001["description"] == "Auth v2"  # Updated from YAML

    def test_copies_all_yaml_fields(self, tmp_path):
        """All fields from YAML flow through — no hardcoded field list."""
        features = [
            {
                "id": "F001",
                "description": "Auth",
                "dependencies": [],
                "spec_ref": "S1",
                "codemap_context": {"files": ["auth.py"], "patterns": ["singleton"]},
                "custom_field": "arbitrary_value",
            },
        ]
        features_dir = _make_features_dir(tmp_path, features)
        fl_path = tmp_path / "feature-list.json"

        result = sync_features_from_yaml(features_dir, fl_path)

        f001 = result["features"][0]
        assert f001["codemap_context"] == {"files": ["auth.py"], "patterns": ["singleton"]}
        assert f001["custom_field"] == "arbitrary_value"

    def test_targeted_sync_single_feature(self, tmp_path):
        """Syncing a specific feature ID only updates that feature."""
        features = [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1"},
            {"id": "F002", "description": "Reg", "dependencies": [], "spec_ref": "S1"},
        ]
        features_dir = _make_features_dir(tmp_path, features)
        fl_path = tmp_path / "feature-list.json"

        # Create initial JSON
        from aah.core.common.io_utils import write_json
        write_json({"features": [
            {"id": "F001", "description": "Auth OLD", "dependencies": [], "spec_ref": "S1", "passes": False},
            {"id": "F002", "description": "Reg OLD", "dependencies": [], "spec_ref": "S1", "passes": True},
        ]}, fl_path)

        # Only sync F001
        result = sync_features_from_yaml(features_dir, fl_path, feature_ids=["F001"])

        f001 = next(f for f in result["features"] if f["id"] == "F001")
        f002 = next(f for f in result["features"] if f["id"] == "F002")
        assert f001["description"] == "Auth"  # Updated
        assert f002["description"] == "Reg OLD"  # Unchanged — not in target
        assert f002["passes"] is True  # Preserved

    def test_new_feature_added_via_targeted_sync(self, tmp_path):
        """A new feature YAML synced by ID gets added with passes=False."""
        features = [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1"},
            {"id": "F003", "description": "New Feature", "dependencies": ["F001"], "spec_ref": "S2"},
        ]
        features_dir = _make_features_dir(tmp_path, features)
        fl_path = tmp_path / "feature-list.json"

        # Existing JSON only has F001
        from aah.core.common.io_utils import write_json
        write_json({"features": [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1", "passes": True},
        ]}, fl_path)

        result = sync_features_from_yaml(features_dir, fl_path, feature_ids=["F003"])

        assert len(result["features"]) == 2
        f003 = next(f for f in result["features"] if f["id"] == "F003")
        assert f003["description"] == "New Feature"
        assert f003["passes"] is False

    def test_full_sync_removes_deleted_yaml(self, tmp_path):
        """Full sync (feature_ids=None) removes features whose YAML was deleted."""
        features = [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1"},
        ]
        features_dir = _make_features_dir(tmp_path, features)
        fl_path = tmp_path / "feature-list.json"

        # JSON has F001 and F002, but only F001 YAML exists
        from aah.core.common.io_utils import write_json
        write_json({"features": [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1", "passes": True},
            {"id": "F002", "description": "Reg", "dependencies": [], "spec_ref": "S1", "passes": False},
        ]}, fl_path)

        result = sync_features_from_yaml(features_dir, fl_path)

        assert len(result["features"]) == 1
        assert result["features"][0]["id"] == "F001"

    def test_idempotent(self, tmp_path):
        """Running sync twice produces the same result."""
        features = [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1"},
            {"id": "F002", "description": "Reg", "dependencies": ["F001"], "spec_ref": "S1"},
        ]
        features_dir = _make_features_dir(tmp_path, features)
        fl_path = tmp_path / "feature-list.json"

        result1 = sync_features_from_yaml(features_dir, fl_path)
        result2 = sync_features_from_yaml(features_dir, fl_path)

        assert result1 == result2


class TestValidateJsonMatchesYamls:
    """Tests for validate_json_matches_yamls."""

    def test_in_sync_returns_no_errors(self, tmp_path):
        """No errors when JSON matches YAMLs exactly."""
        features = [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1"},
        ]
        features_dir = _make_features_dir(tmp_path, features)
        fl_path = tmp_path / "feature-list.json"

        # Create JSON that matches YAML
        from aah.core.common.io_utils import write_json
        write_json({"features": [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1", "passes": False},
        ]}, fl_path)

        errors = validate_json_matches_yamls(features_dir, fl_path)
        assert errors == []

    def test_stale_field_detected(self, tmp_path):
        """Detects when a JSON field doesn't match YAML."""
        features = [
            {"id": "F001", "description": "Auth v2", "dependencies": [], "spec_ref": "S1"},
        ]
        features_dir = _make_features_dir(tmp_path, features)
        fl_path = tmp_path / "feature-list.json"

        # JSON has old description
        from aah.core.common.io_utils import write_json
        write_json({"features": [
            {"id": "F001", "description": "Auth OLD", "dependencies": [], "spec_ref": "S1", "passes": False},
        ]}, fl_path)

        errors = validate_json_matches_yamls(features_dir, fl_path)
        assert len(errors) == 1
        assert "description" in errors[0]
        assert "stale" in errors[0].lower()

    def test_missing_feature_in_json(self, tmp_path):
        """Detects YAML feature not present in JSON."""
        features = [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1"},
            {"id": "F002", "description": "Reg", "dependencies": [], "spec_ref": "S1"},
        ]
        features_dir = _make_features_dir(tmp_path, features)
        fl_path = tmp_path / "feature-list.json"

        # JSON only has F001
        from aah.core.common.io_utils import write_json
        write_json({"features": [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1", "passes": False},
        ]}, fl_path)

        errors = validate_json_matches_yamls(features_dir, fl_path)
        assert len(errors) == 1
        assert "F002" in errors[0]
        assert "missing" in errors[0].lower()

    def test_orphan_feature_in_json(self, tmp_path):
        """Detects JSON feature with no corresponding YAML."""
        features = [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1"},
        ]
        features_dir = _make_features_dir(tmp_path, features)
        fl_path = tmp_path / "feature-list.json"

        # JSON has F001 and F002, but only F001 YAML exists
        from aah.core.common.io_utils import write_json
        write_json({"features": [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1", "passes": False},
            {"id": "F002", "description": "Ghost", "dependencies": [], "spec_ref": "S1", "passes": False},
        ]}, fl_path)

        errors = validate_json_matches_yamls(features_dir, fl_path)
        assert len(errors) == 1
        assert "F002" in errors[0]
        assert "no yaml source" in errors[0].lower()

    def test_no_json_with_yamls_present(self, tmp_path):
        """Error when YAMLs exist but feature-list.json doesn't."""
        features = [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1"},
        ]
        features_dir = _make_features_dir(tmp_path, features)
        fl_path = tmp_path / "feature-list.json"
        # Don't create the JSON file

        errors = validate_json_matches_yamls(features_dir, fl_path)
        assert len(errors) == 1
        assert "does not exist" in errors[0].lower()


class TestValidateSyncIntegrity:
    """Tests for validate_sync_integrity (gate logic)."""

    def test_passes_change_allowed(self, tmp_path):
        """Changing passes field is always allowed."""
        features = [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1"},
        ]
        features_dir = _make_features_dir(tmp_path, features)

        original = {"features": [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1", "passes": False},
        ]}
        proposed = {"features": [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1", "passes": True},
        ]}

        errors = validate_sync_integrity(original, proposed, features_dir)
        assert errors == []

    def test_field_matching_yaml_allowed(self, tmp_path):
        """Field change that matches YAML source is allowed (legitimate sync)."""
        features = [
            {"id": "F001", "description": "Auth v2", "dependencies": [], "spec_ref": "S1"},
        ]
        features_dir = _make_features_dir(tmp_path, features)

        original = {"features": [
            {"id": "F001", "description": "Auth OLD", "dependencies": [], "spec_ref": "S1", "passes": False},
        ]}
        proposed = {"features": [
            {"id": "F001", "description": "Auth v2", "dependencies": [], "spec_ref": "S1", "passes": False},
        ]}

        errors = validate_sync_integrity(original, proposed, features_dir)
        assert errors == []

    def test_field_not_matching_yaml_blocked(self, tmp_path):
        """Field change that doesn't match YAML is blocked (unauthorized edit)."""
        features = [
            {"id": "F001", "description": "Auth v2", "dependencies": [], "spec_ref": "S1"},
        ]
        features_dir = _make_features_dir(tmp_path, features)

        original = {"features": [
            {"id": "F001", "description": "Auth OLD", "dependencies": [], "spec_ref": "S1", "passes": False},
        ]}
        proposed = {"features": [
            {"id": "F001", "description": "HACKED VALUE", "dependencies": [], "spec_ref": "S1", "passes": False},
        ]}

        errors = validate_sync_integrity(original, proposed, features_dir)
        assert len(errors) == 1
        assert "description" in errors[0]
        assert "doesn't match YAML" in errors[0]

    def test_new_feature_with_yaml_allowed(self, tmp_path):
        """Adding a feature that has a YAML source is allowed."""
        features = [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1"},
            {"id": "F002", "description": "Reg", "dependencies": [], "spec_ref": "S1"},
        ]
        features_dir = _make_features_dir(tmp_path, features)

        original = {"features": [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1", "passes": True},
        ]}
        proposed = {"features": [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1", "passes": True},
            {"id": "F002", "description": "Reg", "dependencies": [], "spec_ref": "S1", "passes": False},
        ]}

        errors = validate_sync_integrity(original, proposed, features_dir)
        assert errors == []

    def test_new_feature_without_yaml_blocked(self, tmp_path):
        """Adding a feature without YAML source is blocked."""
        features = [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1"},
        ]
        features_dir = _make_features_dir(tmp_path, features)

        original = {"features": [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1", "passes": True},
        ]}
        proposed = {"features": [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1", "passes": True},
            {"id": "F999", "description": "Ghost", "dependencies": [], "spec_ref": "S1", "passes": False},
        ]}

        errors = validate_sync_integrity(original, proposed, features_dir)
        assert len(errors) == 1
        assert "F999" in errors[0]
        assert "without YAML source" in errors[0]

    def test_feature_removal_blocked(self, tmp_path):
        """Removing a feature is blocked."""
        features = [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1"},
            {"id": "F002", "description": "Reg", "dependencies": [], "spec_ref": "S1"},
        ]
        features_dir = _make_features_dir(tmp_path, features)

        original = {"features": [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1", "passes": True},
            {"id": "F002", "description": "Reg", "dependencies": [], "spec_ref": "S1", "passes": False},
        ]}
        proposed = {"features": [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1", "passes": True},
        ]}

        errors = validate_sync_integrity(original, proposed, features_dir)
        assert len(errors) == 1
        assert "removed" in errors[0].lower()

    def test_codemap_context_field_syncs(self, tmp_path):
        """codemap_context field round-trips correctly YAML → JSON."""
        codemap = {"files": ["src/auth.py"], "patterns": ["factory"], "dependencies": ["flask"]}
        features = [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1", "codemap_context": codemap},
        ]
        features_dir = _make_features_dir(tmp_path, features)

        original = {"features": [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1", "passes": False},
        ]}
        proposed = {"features": [
            {"id": "F001", "description": "Auth", "dependencies": [], "spec_ref": "S1",
             "codemap_context": codemap, "passes": False},
        ]}

        errors = validate_sync_integrity(original, proposed, features_dir)
        assert errors == []
