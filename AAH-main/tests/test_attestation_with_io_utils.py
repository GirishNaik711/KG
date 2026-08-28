"""Integration: write_attested → read_json → verify roundtrip via the
real io_utils write/read path.

Confirms the attestation library is compatible with the existing
write_json_verified flow and that the JSON shape survives through both
ends without bespoke serialization tricks.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from aah.core.common import attestation
from aah.core.common.attestation import SECRET_REL_PATH
from aah.core.common.io_utils import read_json, write_json_verified


def _seed_secret(project_path: Path) -> bytes:
    secret_path = project_path / SECRET_REL_PATH
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    secret_path.write_bytes(secret)
    return secret


class TestRoundtripViaIoUtils:
    def test_write_attested_uses_write_json_verified(self, rapids_project):
        """The library mutates payload then delegates to write_json_verified.

        We check that the resulting file is plain JSON (parseable by read_json)
        and that the attestation block is intact.
        """
        _seed_secret(rapids_project)
        out = rapids_project / ".aah" / "build" / "test-results" / "artifact.json"
        attestation.write_attested(
            {"feature_id": "F001", "verdict": "pass"},
            out,
            project_path=rapids_project,
            command=["aah-run", "aah.core.build.write_qa_report"],
            exit_code=0,
            stdout="",
            stderr="",
            duration_ms=12,
            artifact_name="F001 QA",
        )

        # The file must be parseable as JSON (write_json_verified guarantees this).
        loaded = read_json(out)
        assert loaded["feature_id"] == "F001"
        assert loaded["verdict"] == "pass"

        # Attestation block is present and well-formed.
        assert "attestation" in loaded
        assert loaded["attestation"]["method"] == "subprocess"

        # And the signature verifies under the same secret.
        ok, reason = attestation.verify(loaded, project_path=rapids_project)
        assert ok is True
        assert reason == ""

    def test_post_write_manual_edit_then_verify_fails(self, rapids_project):
        """Simulate the threat model: someone writes the result via the
        official path, then later edits the file directly. The
        attestation must catch this on the next read."""
        _seed_secret(rapids_project)
        out = rapids_project / ".aah" / "build" / "test-results" / "artifact.json"
        attestation.write_attested(
            {"feature_id": "F001", "verdict": "pass"},
            out,
            project_path=rapids_project,
            command=["aah-run", "x"],
            exit_code=0,
            stdout="",
            stderr="",
            duration_ms=1,
        )

        # Now tamper: rewrite the file via the bare write_json (no attestation update).
        from aah.core.common.io_utils import write_json
        loaded = read_json(out)
        loaded["verdict"] = "fail"  # forge the verdict
        write_json(loaded, out)

        # Verify must reject.
        re_loaded = read_json(out)
        ok, reason = attestation.verify(re_loaded, project_path=rapids_project)
        assert ok is False
        # Could be signature_mismatch (most likely)
        assert reason in ("signature_mismatch", "malformed_attestation")

    def test_concurrent_safe_for_distinct_paths(self, rapids_project):
        """Two writes to two different files don't interfere even though
        they share the same project secret."""
        _seed_secret(rapids_project)
        out_a = rapids_project / ".aah" / "build" / "test-results" / "F001.json"
        out_b = rapids_project / ".aah" / "build" / "test-results" / "F002.json"

        attestation.write_attested(
            {"feature_id": "F001"}, out_a, project_path=rapids_project,
            command=["aah-run", "a"], exit_code=0, stdout="", stderr="", duration_ms=1,
        )
        attestation.write_attested(
            {"feature_id": "F002"}, out_b, project_path=rapids_project,
            command=["aah-run", "b"], exit_code=0, stdout="", stderr="", duration_ms=1,
        )

        for path in (out_a, out_b):
            loaded = read_json(path)
            ok, _ = attestation.verify(loaded, project_path=rapids_project)
            assert ok is True
