"""Tests for aah.core.plan.build_dependency_policy.

Phase 3 L5-D (#247). Generator that produces `.rapids/dependency-policy.json`
from project sources (brownfield: package.json/pyproject.toml; greenfield:
manifest.stack_choices.primary template).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aah.core.common.io_utils import read_json, write_json, write_yaml
from aah.core.common.manifest import get_default_manifest, save_manifest
from aah.core.plan.build_dependency_policy import build_dependency_policy


def _seed_brownfield(tmp_path):
    rapids = tmp_path / ".rapids"
    rapids.mkdir()
    manifest = get_default_manifest("brown-test")
    manifest["project_type"] = "brownfield"
    save_manifest(manifest, rapids / "manifest.yaml")
    return tmp_path


def _seed_greenfield(tmp_path, stack_primary):
    rapids = tmp_path / ".rapids"
    rapids.mkdir()
    manifest = get_default_manifest("green-test")
    manifest["project_type"] = "greenfield"
    manifest["stack_choices"] = {"primary": stack_primary}
    save_manifest(manifest, rapids / "manifest.yaml")
    return tmp_path


class TestBrownfieldExtraction:
    def test_extracts_npm_from_package_json(self, tmp_path):
        proj = _seed_brownfield(tmp_path)
        write_json(
            {
                "name": "test",
                "dependencies": {
                    "@mui/material": "^5.4.2",
                    "react": "18.2.0",
                },
                "devDependencies": {
                    "vitest": "^1.0",
                },
            },
            proj / "package.json",
        )
        policy = build_dependency_policy(proj)
        assert "npm" in policy
        assert policy["npm"]["@mui/material"] == "^5"
        assert policy["npm"]["react"] == "^18"
        assert policy["npm"]["vitest"] == "^1"

    def test_extracts_pip_from_requirements(self, tmp_path):
        proj = _seed_brownfield(tmp_path)
        (proj / "requirements.txt").write_text(
            "fastapi==0.110.0\npydantic>=2.5\n# comment\n\n"
        )
        policy = build_dependency_policy(proj)
        assert "pip" in policy
        assert policy["pip"]["fastapi"] == "^0"  # major 0 for 0.110
        assert policy["pip"]["pydantic"] == "^2"


class TestGreenfieldSeeding:
    def test_seeds_node_react_mui5_template(self, tmp_path):
        proj = _seed_greenfield(tmp_path, "node-react-mui5")
        policy = build_dependency_policy(proj)
        assert policy["npm"]["@mui/*"] == "^5"
        assert policy["npm"]["react"] == "^18"

    def test_unknown_stack_yields_empty_policy(self, tmp_path):
        proj = _seed_greenfield(tmp_path, "unknown-stack-xyz")
        policy = build_dependency_policy(proj)
        assert policy == {}


class TestIdempotency:
    def test_repeat_build_produces_same_output(self, tmp_path):
        proj = _seed_brownfield(tmp_path)
        write_json(
            {"dependencies": {"react": "^18.2.0"}},
            proj / "package.json",
        )
        a = build_dependency_policy(proj)
        b = build_dependency_policy(proj)
        assert a == b


class TestJsonOutputShape:
    def test_output_serializes_to_valid_json(self, tmp_path):
        proj = _seed_brownfield(tmp_path)
        write_json(
            {"dependencies": {"react": "^18"}},
            proj / "package.json",
        )
        policy = build_dependency_policy(proj)
        # Round-trip through JSON.
        s = json.dumps(policy)
        assert json.loads(s) == policy
        # Top-level keys are ecosystem names.
        for key in policy:
            assert key in ("npm", "pip", "go", "cargo")
