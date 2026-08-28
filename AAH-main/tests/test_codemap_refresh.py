from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

from aah.core.common.attestation import verify as att_verify
from aah.core.build.codemap_refresh import (
    COMMAND_PREFIX,
    _sha256_file,
    refresh,
)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def codemap_project(git_repo):
    """A project with .aah skeleton, manifest, attestation secret, and
    integration/wave-0 branch checked out + committed.

    We do NOT seed a real codemap.db here — tests opt in to creating it
    when relevant.
    """
    aah_root = git_repo / ".aah"
    (aah_root / "build" / "runtime-results").mkdir(parents=True, exist_ok=True)
    (aah_root / "codebase-intel").mkdir(parents=True, exist_ok=True)
    (aah_root / "audit").mkdir(parents=True, exist_ok=True)

    (aah_root / "manifest.yaml").write_text(
        "project_name: codemap-test\nproject_type: greenfield\ncurrent_phase: implement\n"
    )
    # Attestation secret — required by write_attested.
    (aah_root / "build" / ".attestation-secret").write_bytes(secrets.token_bytes(32))

    env = {**os.environ,
           "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "checkout", "-b", "integration/wave-0"],
                   cwd=git_repo, check=True, capture_output=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True,
                   capture_output=True, env=env)
    subprocess.run(["git", "commit", "-m", "seed"],
                   cwd=git_repo, check=True, capture_output=True, env=env)
    return git_repo


def _seed_codemap_db(project_path: Path, content: bytes = b"fake-codemap-db") -> Path:
    """Create a fake codemap.db with predictable content."""
    db = project_path / ".aah" / "codebase-intel" / "codemap.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(content)
    return db


# ---------------------------------------------------------------------------
# A1, A2 — refresh writes attested artifact with hashes
# ---------------------------------------------------------------------------


class TestRefreshWritesAttestedArtifact:
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="PATH-shim test uses POSIX shebang; Windows needs a different mechanism",
    )
    def test_writes_artifact_when_codemap_shim_exits_127(
        self, codemap_project, tmp_path, monkeypatch
    ):
        """Real failure injection via a shell shim on PATH — avoids
        patching ``_run_codemap_refresh`` (which would be a test double
        per CLAUDE.md).

        Drops a ``codemap`` script onto a temp directory at the front of
        PATH; the script exits 127 with a "command not found" stderr,
        the same shape a missing CLI produces. The subprocess actually
        runs; we observe the real exit code and attested-artifact
        contract.
        """
        shim_dir = tmp_path / "shim_path"
        shim_dir.mkdir()
        shim = shim_dir / "codemap"
        shim.write_text(
            "#!/bin/sh\n"
            "echo 'codemap: command not found' >&2\n"
            "exit 127\n"
        )
        shim.chmod(0o755)
        # Prepend shim_dir to PATH so `codemap` resolves to the shim
        # while git / python / etc. still work via the real PATH.
        monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
        # codemap_scale isn't installed in the test venv anyway, so the
        # import fallback also fails — the project's actual posture.

        rc = refresh(codemap_project, 0)
        artifact = (codemap_project / ".aah" / "build" / "runtime-results"
                    / "wave-0-codemap.json")
        assert artifact.exists()
        payload = json.loads(artifact.read_text())
        assert payload["wave"] == 0
        assert payload["operation"] == "refresh"
        assert rc == 1, f"expected 1 (codemap shim exits 127); got {rc}"
        assert payload["overall_passed"] is False
        # Real exit code from the real subprocess.
        assert payload["exit_code"] == 127
        # Real stderr from the shim.
        assert "command not found" in payload["stderr_tail"]

    def test_artifact_passes_attestation_with_correct_prefix(self, codemap_project):
        refresh(codemap_project, 0)
        artifact_path = (codemap_project / ".aah" / "build" / "runtime-results"
                         / "wave-0-codemap.json")
        payload = json.loads(artifact_path.read_text())
        ok, reason = att_verify(
            payload, project_path=codemap_project,
            expected_command_prefix=COMMAND_PREFIX,
        )
        assert ok is True, f"attestation failed: {reason}"

    def test_payload_captures_db_hash(self, codemap_project):
        # Seed a codemap.db so the post-state hash is non-empty.
        db = _seed_codemap_db(codemap_project)
        expected_hash = _sha256_file(db)

        refresh(codemap_project, 0)
        artifact_path = (codemap_project / ".aah" / "build" / "runtime-results"
                         / "wave-0-codemap.json")
        payload = json.loads(artifact_path.read_text())
        # The fake db is unchanged by the refresh failure, so post == pre.
        assert payload["codemap_db_sha256_post"] == expected_hash


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------


class TestSha256File:
    def test_returns_empty_for_missing(self, tmp_path):
        assert _sha256_file(tmp_path / "nope") == ""

    def test_streams_large_file(self, tmp_path):
        """64KB-chunked hashing should agree with stdlib's hashlib for
        files larger than the chunk size."""
        import hashlib
        data = secrets.token_bytes(200_000)
        f = tmp_path / "big.bin"
        f.write_bytes(data)
        assert _sha256_file(f) == hashlib.sha256(data).hexdigest()
