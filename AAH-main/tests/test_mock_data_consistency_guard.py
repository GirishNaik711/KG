"""Tests for aah.core.guards.mock_data_consistency_guard.

Phase 3 L5-B (#247). PostToolUse(Write|Edit|MultiEdit) guard that
rejects writes to fixture/mock/seed JSON files introducing entity
names not in `.aah/entity-registry.json`. PartsPulse #5/#9 (mock
data with hardcoded "Plant A"/"Plant B") is the regression case.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

from aah.core.common.io_utils import write_json
from aah.core.common.manifest import get_default_manifest, save_manifest


@pytest.fixture
def project_with_registry(tmp_path):
    aah_root = tmp_path / ".aah"
    aah_root.mkdir()
    (aah_root / "build").mkdir()
    manifest = get_default_manifest("mock-data-test")
    save_manifest(manifest, aah_root / "manifest.yaml")
    (aah_root / "build" / ".attestation-secret").write_bytes(secrets.token_bytes(32))
    write_json(
        {"entities": ["plant", "tenant", "memory_entry"]},
        aah_root / "entity-registry.json",
    )
    return tmp_path


def _run_guard(
    file_path: Path | str,
    cwd: Path,
    *,
    extra_env: dict | None = None,
    tool_name: str = "Write",
) -> tuple[int, str]:
    """Invoke the guard with a Write/Edit hook input naming `file_path`."""
    proc_env = {**os.environ}
    proc_env.pop("AAH_CONTEXT", None)
    if extra_env:
        proc_env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-m", "aah.core.guards.mock_data_consistency_guard"],
        input=json.dumps({
            "tool_name": tool_name,
            "tool_input": {"file_path": str(file_path)},
        }),
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=proc_env,
    )
    return proc.returncode, proc.stderr


# ---------------------------------------------------------------------------


class TestPathMatching:
    def test_matches_mock_glob(self, project_with_registry):
        f = project_with_registry / "data" / "mock-data.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        write_json({"items": [{"name": "Customer A"}]}, f)
        code, _ = _run_guard(f, cwd=project_with_registry)
        assert code == 2  # offending entity

    def test_matches_fixtures_dir(self, project_with_registry):
        f = project_with_registry / "tests" / "fixtures" / "users.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        write_json({"name": "Outsider"}, f)
        code, _ = _run_guard(f, cwd=project_with_registry)
        assert code == 2

    def test_skips_non_fixture_files(self, project_with_registry):
        f = project_with_registry / "src" / "main.py"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("print('hi')")
        code, _ = _run_guard(f, cwd=project_with_registry)
        assert code == 0


# ---------------------------------------------------------------------------


class TestEntityValidation:
    def test_unregistered_entity_blocks(self, project_with_registry):
        """The flagship case: write a fixture with "Customer A" — not in registry."""
        f = project_with_registry / "data" / "mock-customers.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        write_json({"items": [{"name": "Customer A"}, {"name": "Vendor B"}]}, f)
        code, stderr = _run_guard(f, cwd=project_with_registry)
        assert code == 2
        assert "Customer" in stderr or "Vendor" in stderr

    def test_registered_entity_allows(self, project_with_registry):
        """Plants are registered — fixture is fine."""
        f = project_with_registry / "data" / "mock-plants.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        write_json({"items": [{"name": "plant-1"}, {"name": "plant-2"}]}, f)
        code, stderr = _run_guard(f, cwd=project_with_registry)
        assert code == 0, f"registered entities should pass; stderr={stderr!r}"

    def test_missing_registry_allows(self, tmp_path):
        """No registry → guard is a no-op."""
        aah_root = tmp_path / ".aah"
        aah_root.mkdir()
        manifest = get_default_manifest("no-registry")
        save_manifest(manifest, aah_root / "manifest.yaml")
        f = tmp_path / "data" / "mock.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        write_json({"name": "AnythingGoes"}, f)
        code, _ = _run_guard(f, cwd=tmp_path)
        assert code == 0


# ---------------------------------------------------------------------------


class TestFuzzyMatching:
    def test_plant_a_matches_plant(self, project_with_registry):
        """The PartsPulse case: 'Plant A' must match registered canonical 'plant'."""
        f = project_with_registry / "data" / "mock-data.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        write_json(
            {"items": [{"name": "Plant A"}, {"name": "Plant B"}, {"name": "plants"}]},
            f,
        )
        code, stderr = _run_guard(f, cwd=project_with_registry)
        assert code == 0, (
            f"'Plant A' must fuzzy-match registered 'plant'; stderr={stderr!r}"
        )


# ---------------------------------------------------------------------------


class TestEnvBypass:
    def test_bypass_env_var_allows(self, project_with_registry):
        f = project_with_registry / "data" / "mock-data.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        write_json({"items": [{"name": "Outsider"}]}, f)
        code, _ = _run_guard(
            f,
            cwd=project_with_registry,
            extra_env={"AAH_MOCK_DATA_BYPASS": "1"},
        )
        assert code == 0


# ---------------------------------------------------------------------------


class TestRobustness:
    def test_malformed_json_passes(self, project_with_registry):
        """Not-parseable JSON is out of scope for this guard."""
        f = project_with_registry / "data" / "mock-data.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{not valid json")
        code, _ = _run_guard(f, cwd=project_with_registry)
        assert code == 0

    def test_missing_file_passes(self, project_with_registry):
        """If the file doesn't exist on disk (delete intervened), pass through."""
        f = project_with_registry / "data" / "ghost-fixtures.json"
        # Don't create it.
        code, _ = _run_guard(f, cwd=project_with_registry)
        assert code == 0
