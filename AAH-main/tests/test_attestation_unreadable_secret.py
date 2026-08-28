"""Phase 1.5 / Finding 3: verify() must surface unreadable secrets cleanly.

When the attestation secret file exists but cannot be read (permissions,
partial write, FS corruption), verify() must return
(False, REASON_UNREADABLE_SECRET) instead of letting the OSError
propagate up to the caller (the orchestrator hot path in Phase 2).
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import pytest

from aah.core.common import attestation
from aah.core.common.attestation import (
    REASON_MISSING_SECRET,
    REASON_UNREADABLE_SECRET,
    SECRET_REL_PATH,
)
from aah.core.common.io_utils import read_json


def _seed_secret(project_path: Path) -> Path:
    secret_path = project_path / SECRET_REL_PATH
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_bytes(secrets.token_bytes(32))
    return secret_path


def _make_signed_payload(rapids_project: Path) -> dict:
    """Write+verify a payload, then return the loaded dict for further testing.

    We need a payload signed under the secret BEFORE we make the secret
    unreadable, so the signature itself is valid — verify() should fail
    on the secret-read step, not on anything earlier.
    """
    out = rapids_project / ".aah" / "build" / "test-results" / "F001.json"
    attestation.write_attested(
        {"feature_id": "F001", "verdict": "pass"},
        out,
        project_path=rapids_project,
        command=["aah-run", "aah.core.build.write_qa_report"],
        exit_code=0,
        stdout="",
        stderr="",
        duration_ms=1,
    )
    return read_json(out)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX chmod 000 has no equivalent on Windows; permission tests are POSIX-only",
)
class TestUnreadableSecret:
    def test_chmod_000_returns_unreadable_secret(self, rapids_project):
        secret_path = _seed_secret(rapids_project)
        loaded = _make_signed_payload(rapids_project)

        original_mode = secret_path.stat().st_mode & 0o777
        os.chmod(secret_path, 0o000)
        try:
            ok, reason = attestation.verify(loaded, project_path=rapids_project)
        finally:
            # Restore so tmp_path teardown doesn't choke on a 000-mode file.
            os.chmod(secret_path, original_mode)

        assert ok is False
        assert reason == REASON_UNREADABLE_SECRET, (
            f"expected REASON_UNREADABLE_SECRET, got {reason!r}"
        )

    def test_missing_secret_still_returns_missing_not_unreadable(self, rapids_project):
        """Regression: the more-specific FileNotFoundError catch must
        still fire before the broader OSError catch."""
        _seed_secret(rapids_project)
        loaded = _make_signed_payload(rapids_project)
        # Now delete the secret entirely.
        secret_path = rapids_project / SECRET_REL_PATH
        secret_path.unlink()

        ok, reason = attestation.verify(loaded, project_path=rapids_project)
        assert ok is False
        assert reason == REASON_MISSING_SECRET, (
            f"expected REASON_MISSING_SECRET (more specific), got {reason!r}"
        )

    def test_unreadable_secret_constant_is_exported(self):
        """Phase 2 readers will dispatch on this constant — assert it's stable."""
        assert REASON_UNREADABLE_SECRET == "unreadable_secret"
