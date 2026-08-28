"""Phase 2A.1 / Finding 7: smoke tests for the non-attested helper.

write_test_dockerfile.py persists Dockerfile.test on behalf of the
aah-runtime-validator subagent. The Dockerfile content arrives on stdin
(the agent composes it; the helper just writes it). This file is NOT
a verification artifact — Docker reads it during build, no orchestrator
gate consults it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from aah.core.common.manifest import get_default_manifest, save_manifest


@pytest.fixture
def project(tmp_path):
    """Minimal .aah skeleton — only manifest is needed."""
    aah_root = tmp_path / ".aah"
    aah_root.mkdir()
    save_manifest(get_default_manifest("test-proj"), aah_root / "manifest.yaml")
    return tmp_path


def _run(project_path: Path, content: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "aah.core.build.write_test_dockerfile",
         "--project-path", str(project_path)],
        input=content,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )


def test_writes_dockerfile_from_stdin(project):
    content = "FROM python:3.11-slim\nWORKDIR /app\nCOPY . .\nCMD python app.py\n"
    proc = _run(project, content)
    assert proc.returncode == 0, f"unexpected failure: stderr={proc.stderr!r}"

    out = project / ".aah" / "build" / "Dockerfile.test"
    assert out.exists()
    persisted = out.read_text()
    assert "FROM python:3.11-slim" in persisted
    assert "CMD python app.py" in persisted
    # The file is plain text, not JSON — and definitely not attested.
    # If a future change tries to wrap it in JSON, this test breaks.
    assert not persisted.startswith("{"), (
        "Dockerfile.test must be plain Dockerfile content, not a JSON wrapper"
    )


def test_empty_stdin_fails(project):
    proc = _run(project, "")
    assert proc.returncode == 1
    assert "empty" in proc.stderr.lower()


def test_whitespace_only_stdin_fails(project):
    proc = _run(project, "   \n\t\n")
    assert proc.returncode == 1
    assert "empty" in proc.stderr.lower()
