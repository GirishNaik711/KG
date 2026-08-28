"""Functional tests for mandatory build feature-contract validation.

NO MOCKS: real temp .aah/ project, real .md frontmatter contracts, real
manifest.yaml, invoked through the real CLI via subprocess. Fixtures are
defined locally in this file (no conftest dependency).
"""

import json
import textwrap
from pathlib import Path

import pytest

from tests.support.aah_project import AAHProjectBuilder


def _write_md(features_dir: Path, feature_id: str, frontmatter_yaml: str, body: str = "") -> Path:
    """Write a feature .md file: --- <yaml> --- <body>."""
    features_dir.mkdir(parents=True, exist_ok=True)
    path = features_dir / f"{feature_id}.md"
    content = "---\n" + textwrap.dedent(frontmatter_yaml).strip("\n") + "\n---\n" + body
    path.write_text(content, encoding="utf-8")
    return path


def _make_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create a real .aah/ project skeleton.

    Returns (aah_dir, features_dir, output_json_path).
    """
    project = AAHProjectBuilder.create(tmp_path, git=False).manifest(
        project_name="test",
        project_type="greenfield",
        current_phase="plan",
    )
    return project.aah, project.aah / "plan" / "features", project.aah / "feature-list.json"


_KNOWLEDGE = """\
    title: Test feature
    module_ref: MOD-TEST
    layers: [backend]
    file_scope: [src/app.py]
    knowledge_used:
      knowledge_folder: null
      domain_brief: null"""


def _valid_contract(feature_id: str = "F001") -> str:
    return f"""
    id: {feature_id}
    spec_ref: SPEC-001
    description: A valid feature contract
    dependencies: []
    status: pending
    acceptance_criteria:
      - id: AC1
        description: Users can log in
      - id: AC2
        description: Tokens expire
    test_cases:
      - id: TC1
        covers: [AC1]
        description: successful login
      - id: TC2
        covers: [AC1, AC2]
        description: token expiry
{_KNOWLEDGE}
    """


def _run_build(features_dir: Path, output: Path, extra_env: dict | None = None):
    """Invoke the real build_feature_list CLI. Returns CompletedProcess."""
    project = AAHProjectBuilder(features_dir.parent.parent.parent)
    if extra_env:
        import os
        old = {key: os.environ.get(key) for key in extra_env}
        os.environ.update(extra_env)
        try:
            return project.run_module(
                "aah.cli", "run", "core.plan.build_feature_list",
                "--features-dir", str(features_dir), "--output", str(output),
            )
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    return project.run_module(
        "aah.cli", "run", "core.plan.build_feature_list",
        "--features-dir", str(features_dir), "--output", str(output),
    )


def _run_sync(features_dir: Path, output: Path):
    """Invoke the real feature_list sync CLI. Returns CompletedProcess."""
    return AAHProjectBuilder(features_dir.parent.parent.parent).run_module(
        "aah.cli", "run", "core.common.feature_list", "sync",
        "--path", str(output), "--features-dir", str(features_dir),
    )










def _string_criteria_contract(feature_id: str = "F001") -> str:
    return f"""
    id: {feature_id}
    spec_ref: SPEC-001
    description: legacy string ACs
    dependencies: []
    status: pending
    acceptance_criteria:
      - Users can log in
      - Tokens expire
    test_cases:
      - id: TC1
        covers: [AC1]
{_KNOWLEDGE}
    """




def test_structure_unchanged_only_added_fields(tmp_path):
    aah, features_dir, output = _make_project(tmp_path)
    _write_md(features_dir, "F001", _valid_contract("F001"))

    result = _run_build(features_dir, output)
    assert result.returncode == 0, result.stderr

    data = json.loads(output.read_text())
    assert set(data.keys()) == {"features"}
    entry = data["features"][0]
    source_keys = {
        "id", "title", "module_ref", "spec_ref", "description", "layers",
        "file_scope", "dependencies", "status",
        "acceptance_criteria", "test_cases", "knowledge_used",
    }
    assert set(entry.keys()) == source_keys | {"passes"}
    assert "passes" in entry


def test_build_cli_sync_path_gate_fails_and_preserves_json(tmp_path):
    """Pre-existing feature-list.json => build_feature_list takes the sync
    path. Editing a .md to an invalid v2 contract must fail closed AND leave
    the valid JSON un-corrupted."""
    aah, features_dir, output = _make_project(tmp_path)
    _write_md(features_dir, "F001", _valid_contract("F001"))

    first = _run_build(features_dir, output)
    assert first.returncode == 0, first.stderr
    assert output.exists()
    valid_bytes = output.read_bytes()

    # v2 (lean/TDD): acceptance_criteria/test_cases are no longer required and
    # there is no AC↔test coverage premise. The reduced validator still fails
    # closed on structurally-broken legacy content (here, a duplicate test-case
    # id), which is what this test now exercises.
    bad = f"""
    id: F001
    spec_ref: SPEC-001
    description: now invalid
    dependencies: []
    status: pending
    acceptance_criteria:
      - id: AC1
        description: one
    test_cases:
      - id: TC1
        covers: [AC1]
      - id: TC1
        covers: [AC1]
{_KNOWLEDGE}
    """
    _write_md(features_dir, "F001", bad)

    second = _run_build(features_dir, output)
    assert second.returncode != 0
    assert "duplicate test-case id 'TC1'" in second.stderr
    assert output.read_bytes() == valid_bytes


def test_feature_list_sync_cli_fails_closed(tmp_path):
    """The `feature_list sync` CLI (separate writer) must also fail closed and
    not overwrite the existing JSON with the invalid contract."""
    aah, features_dir, output = _make_project(tmp_path)
    _write_md(features_dir, "F001", _valid_contract("F001"))

    built = _run_build(features_dir, output)
    assert built.returncode == 0, built.stderr
    valid_bytes = output.read_bytes()

    bad = f"""
    id: F001
    spec_ref: SPEC-001
    description: invalid dup ac
    dependencies: []
    status: pending
    acceptance_criteria:
      - id: AC1
        description: one
      - id: AC1
        description: two
    test_cases:
      - id: TC1
        covers: [AC1]
{_KNOWLEDGE}
    """
    _write_md(features_dir, "F001", bad)

    result = _run_sync(features_dir, output)
    assert result.returncode != 0
    assert "duplicate acceptance-criterion id 'AC1'" in result.stderr
    assert output.read_bytes() == valid_bytes
