"""Tests for aah.core.plan.build_entity_registry.

Phase 3 L5-D (#247). Generator that produces `.rapids/entity-registry.json`
from feature YAMLs' optional `entities:` blocks (brownfield: also
supplements from existing fixtures).
"""

from __future__ import annotations

import pytest

from aah.core.common.io_utils import read_json, write_json, write_yaml
from aah.core.common.manifest import get_default_manifest, save_manifest
from aah.core.plan.build_entity_registry import build_entity_registry


def _seed_project(tmp_path, project_type="greenfield"):
    rapids = tmp_path / ".rapids"
    (rapids / "plan" / "features").mkdir(parents=True)
    manifest = get_default_manifest("entity-test")
    manifest["project_type"] = project_type
    save_manifest(manifest, rapids / "manifest.yaml")
    return tmp_path


class TestAggregation:
    def test_aggregates_entities_across_feature_yamls(self, tmp_path):
        proj = _seed_project(tmp_path)
        write_yaml(
            {
                "id": "F001", "description": "x", "dependencies": [],
                "acceptance_criteria": ["AC1"],
                "test_cases": [{"id": "TC1", "covers": ["AC1"]}],
                "entities": [{"name": "plant"}, {"name": "tenant"}],
            },
            proj / ".rapids" / "plan" / "features" / "F001.yaml",
        )
        write_yaml(
            {
                "id": "F002", "description": "y", "dependencies": [],
                "acceptance_criteria": ["AC1"],
                "test_cases": [{"id": "TC1", "covers": ["AC1"]}],
                "entities": [{"name": "memory_entry"}],
            },
            proj / ".rapids" / "plan" / "features" / "F002.yaml",
        )
        registry = build_entity_registry(proj)
        assert "memory_entry" in registry["entities"]
        assert "plant" in registry["entities"]
        assert "tenant" in registry["entities"]

    def test_dedups_and_lowercases(self, tmp_path):
        proj = _seed_project(tmp_path)
        write_yaml(
            {
                "id": "F001", "description": "x", "dependencies": [],
                "acceptance_criteria": ["AC1"],
                "test_cases": [{"id": "TC1", "covers": ["AC1"]}],
                "entities": [
                    {"name": "Plant"},
                    {"name": "PLANT"},
                    {"name": "plant"},
                ],
            },
            proj / ".rapids" / "plan" / "features" / "F001.yaml",
        )
        registry = build_entity_registry(proj)
        # All three variants reduce to a single entry.
        assert registry["entities"].count("plant") == 1
        # All entries are lowercased.
        for e in registry["entities"]:
            assert e == e.lower()


class TestBrownfieldSupplement:
    def test_supplements_from_fixtures_in_brownfield(self, tmp_path):
        proj = _seed_project(tmp_path, project_type="brownfield")
        # No feature YAMLs with entities. Add a fixture file.
        fixtures = proj / "tests" / "fixtures"
        fixtures.mkdir(parents=True)
        write_json(
            {"items": [{"name": "Customer"}, {"name": "Vendor"}]},
            fixtures / "data-fixtures.json",
        )
        registry = build_entity_registry(proj)
        assert "customer" in registry["entities"]
        assert "vendor" in registry["entities"]

    def test_greenfield_does_not_supplement_from_fixtures(self, tmp_path):
        """Greenfield projects skip the brownfield-fixture walk —
        fixtures don't exist yet for greenfield."""
        proj = _seed_project(tmp_path, project_type="greenfield")
        fixtures = proj / "tests" / "fixtures"
        fixtures.mkdir(parents=True)
        write_json(
            {"items": [{"name": "Customer"}]},
            fixtures / "fixtures.json",
        )
        registry = build_entity_registry(proj)
        assert "customer" not in registry["entities"]
