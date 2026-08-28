"""Tests for aah.core.build.regenerate_attestation_secret.

This is the SessionStart hook that ensures a durable project-local HMAC key.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aah.core.common.attestation import (
    REASON_UNREADABLE_SECRET,
    SECRET_REL_PATH,
    verify,
    write_attested,
)
from aah.core.common.io_utils import read_json


def _run_module(cwd: Path, env: dict | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "aah.core.build.regenerate_attestation_secret"],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env={**os.environ, **(env or {})},
    )
    return proc.returncode, proc.stdout, proc.stderr


@pytest.fixture
def attestation_project(tmp_path: Path) -> Path:
    """Minimal live-format project independent of legacy scaffold fixtures."""
    project = tmp_path / "project"
    aah_path = project / ".aah"
    aah_path.mkdir(parents=True)
    (aah_path / "manifest.yaml").write_text(
        "project_name: attestation-test\n"
        "project_type: greenfield\n"
        "execution_mode: aah_root\n",
        encoding="utf-8",
    )
    return project


def _syntactically_valid_attestation() -> dict:
    """Attestation-shaped probe that reaches the fail-closed key reader."""
    return {
        "attestation": {
            "method": "subprocess",
            "command": ["aah-run", "probe"],
            "exit_code": 0,
            "stdout_sha256": "0" * 64,
            "stderr_sha256": "0" * 64,
            "duration_ms": 1,
            "host_pid": 1,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "session_secret_sha256": "0" * 64,
            "signature": "0" * 64,
            "schema_version": 1,
        }
    }


class TestRegenerateAttestationSecret:
    def test_creates_secret_in_project(self, attestation_project):
        secret_path = attestation_project / SECRET_REL_PATH
        assert not secret_path.exists()

        code, _, _ = _run_module(attestation_project)
        assert code == 0
        assert secret_path.exists()
        # 32 bytes of entropy.
        assert len(secret_path.read_bytes()) == 32

    def test_preserves_key_and_timestamp_on_re_run(self, attestation_project):
        _run_module(attestation_project)
        secret_path = attestation_project / SECRET_REL_PATH
        first = secret_path.read_bytes()
        first_mtime = secret_path.stat().st_mtime_ns

        _run_module(attestation_project)
        second = secret_path.read_bytes()
        second_mtime = secret_path.stat().st_mtime_ns

        assert first == second, "SessionStart must preserve the project key"
        assert first_mtime == second_mtime, "valid existing key must not be rewritten"
        assert len(second) == 32

    def test_attested_evidence_survives_restart(self, attestation_project):
        code, _, _ = _run_module(attestation_project)
        assert code == 0
        secret_path = attestation_project / SECRET_REL_PATH
        first = secret_path.read_bytes()

        result_path = (
            attestation_project / ".aah" / "build" / "test-results" / "restart.json"
        )
        write_attested(
            {"passed": True},
            result_path,
            project_path=attestation_project,
            command=["aah-run", "test-writer"],
            exit_code=0,
            stdout="passed",
            stderr="",
            duration_ms=1,
            artifact_name="restart evidence",
        )

        code, _, _ = _run_module(attestation_project)
        assert code == 0
        assert secret_path.read_bytes() == first
        assert verify(
            read_json(result_path),
            project_path=attestation_project,
            expected_command_prefix=["aah-run", "test-writer"],
        ) == (True, "")

    def test_concurrent_initialization_preserves_one_key(self, attestation_project):
        def start_hook(_index: int) -> tuple[int, str, str]:
            return _run_module(attestation_project)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(start_hook, range(16)))

        assert all(code == 0 for code, _, _ in results)
        assert all(not stderr for _, _, stderr in results)
        secret_path = attestation_project / SECRET_REL_PATH
        key = secret_path.read_bytes()
        assert len(key) == 32

        # A further startup must observe the same winning key.
        _run_module(attestation_project)
        assert secret_path.read_bytes() == key

    def test_malformed_existing_key_is_not_replaced(self, attestation_project):
        secret_path = attestation_project / SECRET_REL_PATH
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret_path.write_bytes(b"too-short")

        code, _, stderr = _run_module(attestation_project)

        assert code == 0  # SessionStart continues, evidence operations fail closed.
        assert "unavailable or invalid" in stderr
        assert secret_path.read_bytes() == b"too-short"
        assert verify(
            _syntactically_valid_attestation(),
            project_path=attestation_project,
        ) == (False, REASON_UNREADABLE_SECRET)

    def test_symlink_key_is_not_followed_or_replaced(self, attestation_project, tmp_path):
        if not hasattr(os, "symlink"):
            pytest.skip("symlinks are not supported on this platform")
        target = tmp_path / "outside-key"
        target.write_bytes(b"x" * 32)
        secret_path = attestation_project / SECRET_REL_PATH
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            secret_path.symlink_to(target)
        except OSError as exc:
            pytest.skip(f"symlink creation is unavailable: {exc}")

        code, _, stderr = _run_module(attestation_project)

        assert code == 0
        assert "unavailable or invalid" in stderr
        assert secret_path.is_symlink()
        assert target.read_bytes() == b"x" * 32

    def test_non_regular_key_path_is_not_replaced(self, attestation_project):
        secret_path = attestation_project / SECRET_REL_PATH
        secret_path.mkdir(parents=True)

        code, _, stderr = _run_module(attestation_project)

        assert code == 0
        assert "unavailable or invalid" in stderr
        assert secret_path.is_dir()

    def test_mode_is_0600_on_posix(self, attestation_project):
        if sys.platform == "win32":
            return  # POSIX permissions don't apply
        _run_module(attestation_project)
        secret_path = attestation_project / SECRET_REL_PATH
        mode = secret_path.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    def test_silent_no_op_when_no_project(self, tmp_path):
        # No .aah/manifest.yaml anywhere up the tree (we're in tmp_path).
        # With no project resolvable, the script exits 0 without writing.
        code, _, stderr = _run_module(tmp_path)
        assert code == 0
        # No secret file should have been created.
        assert not (tmp_path / SECRET_REL_PATH).exists()

    def test_skips_when_delegated(self, attestation_project):
        # Mark the project as delegated.
        from aah.core.common.manifest import load_manifest, save_manifest
        manifest_path = attestation_project / ".aah" / "manifest.yaml"
        manifest = load_manifest(manifest_path)
        manifest["execution_mode"] = "delegated"
        save_manifest(manifest, manifest_path)

        secret_path = attestation_project / SECRET_REL_PATH
        assert not secret_path.exists()

        code, _, _ = _run_module(attestation_project)
        assert code == 0
        # Delegated mode → no-op, no secret file.
        assert not secret_path.exists()

    def test_creates_build_dir_if_missing(self, attestation_project):
        # Confirms mkdir(parents=True, exist_ok=True) handles a missing
        # .aah/build/ dir. We use a sibling project root with no
        # standard skeleton instead of altering the fixture's skeleton.
        import shutil
        from aah.core.common.manifest import get_default_manifest, save_manifest

        project = attestation_project.parent / "minimal-project"
        project.mkdir()
        # Minimal .aah/manifest.yaml so resolve_project_path finds it,
        # but no build/ subdir.
        aah_root = project / ".aah"
        aah_root.mkdir()
        save_manifest(get_default_manifest("minimal", "greenfield"), aah_root / "manifest.yaml")

        impl_dir = project / ".aah" / "build"
        assert not impl_dir.exists()

        code, _, _ = _run_module(project)
        assert code == 0
        assert (project / SECRET_REL_PATH).exists()
        # Cleanup so other tests in the same session don't see this project.
        shutil.rmtree(project)

    def test_does_not_block_session_start_on_oserror(self, attestation_project, monkeypatch):
        # We can't easily simulate a write failure inside a subprocess,
        # but we can confirm the in-process main() catches OSError and
        # exits 0 (via the explicit try/except).
        from aah.core.build import regenerate_attestation_secret as m

        def _boom(_pp):
            raise OSError("disk full")

        monkeypatch.setattr(m, "ensure_attestation_secret", _boom)

        # Patch resolve_project_path to return our project so we don't
        # hit the early "no project" return.
        from aah.core.common import config as cfg
        monkeypatch.setattr(cfg, "resolve_project_path", lambda *a, **k: attestation_project)

        # Patch exit_if_delegated to be a no-op.
        from aah.core.guards import delegation_guard
        monkeypatch.setattr(delegation_guard, "exit_if_delegated", lambda: None)

        try:
            m.main()
        except SystemExit as e:
            assert e.code == 0
        else:
            raise AssertionError("main() did not call sys.exit")
