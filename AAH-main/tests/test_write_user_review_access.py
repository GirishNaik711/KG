"""Phase 2A.1 / Finding 7: smoke tests for the non-attested helper.

write_user_review_access.py persists wave-N-user-review-access.json
on behalf of the aah-runtime-validator subagent (which has no Write tool).
The file is human-facing only — NOT a verification artifact, NOT
attested. These tests pin that contract.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from aah.core.common.io_utils import read_json
from aah.core.common.manifest import get_default_manifest, save_manifest


@pytest.fixture
def project(tmp_path):
    """Minimal .aah skeleton — only manifest is needed (the helper
    does not read attestation or anything else)."""
    aah_root = tmp_path / ".aah"
    aah_root.mkdir()
    save_manifest(get_default_manifest("test-proj"), aah_root / "manifest.yaml")
    return tmp_path


def _run(project_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "aah.core.build.write_user_review_access",
         "--project-path", str(project_path), *args],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )


def test_writes_access_info_with_status_running(project):
    proc = _run(
        project,
        "--wave", "0",
        "--access-info-json",
        json.dumps({
            "access_url": "http://localhost:8000",
            "access_type": "http",
            "port": 8000,
            "container_name": "aah-app-test",
        }),
    )
    assert proc.returncode == 0, f"unexpected failure: stderr={proc.stderr!r}"

    out = project / ".aah/build/checkpoint-results/wave-0-user-review-access.json"
    loaded = read_json(out)
    # Wave + caller-provided fields preserved.
    assert loaded["wave"] == 0
    assert loaded["access_url"] == "http://localhost:8000"
    assert loaded["port"] == 8000
    assert loaded["container_name"] == "aah-app-test"
    # Helper-added fields.
    assert loaded["status"] == "running"
    assert "timestamp" in loaded
    # Critically: this file is deliberately NOT attested (no gate reads it).
    assert "attestation" not in loaded, (
        "user-review-access file must not carry an attestation block — "
        "it's a human-facing hand-off, not a gated artifact."
    )


def test_invalid_json_fails(project):
    proc = _run(
        project,
        "--wave", "0",
        "--access-info-json", "not json",
    )
    assert proc.returncode == 1
    assert "Error parsing" in proc.stderr


def test_array_rejected(project):
    proc = _run(
        project,
        "--wave", "0",
        "--access-info-json", "[]",
    )
    assert proc.returncode == 1
    assert "must be a JSON object" in proc.stderr
