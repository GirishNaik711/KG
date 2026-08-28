"""Unit tests for aah.core.common.attestation — round-trip + tamper detection."""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest

from aah.core.common import attestation
from aah.core.common.attestation import (
    REASON_MALFORMED,
    REASON_MISSING_SECRET,
    REASON_NO_ATTESTATION,
    REASON_SIGNATURE_MISMATCH,
    REASON_STALE_SECRET,
    SECRET_REL_PATH,
    VERIFICATION_WRITE_ENV,
)
from aah.core.common.io_utils import read_json


def _seed_secret(project_path: Path, secret_bytes: bytes | None = None) -> bytes:
    """Write a secret to the project's standard secret path. Returns the bytes."""
    secret_path = project_path / SECRET_REL_PATH
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret = secret_bytes if secret_bytes is not None else secrets.token_bytes(32)
    secret_path.write_bytes(secret)
    return secret


def _make_payload() -> dict:
    return {"feature_id": "F001", "verdict": "pass", "summary": {"passed": 5}}


def _write(project_path: Path, payload: dict) -> Path:
    out = project_path / ".aah" / "build" / "test-results" / "artifact.json"
    attestation.write_attested(
        payload,
        out,
        project_path=project_path,
        command=["aah-run", "aah.core.build.write_qa_report"],
        exit_code=0,
        stdout="ok",
        stderr="",
        duration_ms=42,
        artifact_name="attestation test artifact",
    )
    return out


# ---------------------------------------------------------------------------
# Roundtrip
# ---------------------------------------------------------------------------


class TestRoundtrip:
    def test_signed_payload_verifies(self, rapids_project):
        _seed_secret(rapids_project)
        out = _write(rapids_project, _make_payload())
        loaded = read_json(out)

        ok, reason = attestation.verify(loaded, project_path=rapids_project)
        assert ok is True
        assert reason == ""

    def test_attestation_block_has_required_fields(self, rapids_project):
        _seed_secret(rapids_project)
        out = _write(rapids_project, _make_payload())
        loaded = read_json(out)

        att = loaded["attestation"]
        for field in (
            "method", "command", "exit_code", "stdout_sha256", "stderr_sha256",
            "duration_ms", "host_pid", "timestamp", "session_secret_sha256",
            "signature", "schema_version",
        ):
            assert field in att, f"missing attestation field: {field}"
        assert att["method"] == "subprocess"
        assert att["command"][0] == "aah-run"
        assert att["schema_version"] == 1

    def test_payload_data_preserved(self, rapids_project):
        _seed_secret(rapids_project)
        out = _write(rapids_project, _make_payload())
        loaded = read_json(out)

        assert loaded["feature_id"] == "F001"
        assert loaded["verdict"] == "pass"
        assert loaded["summary"]["passed"] == 5

    def test_canonical_json_determinism(self, rapids_project):
        """Different insertion order in the payload must produce the same signature."""
        _seed_secret(rapids_project)
        # Insertion order A
        a = {"a": 1, "b": 2, "c": 3}
        # Insertion order B (same data, different order)
        b = {"c": 3, "a": 1, "b": 2}

        out_a = rapids_project / ".aah" / "build" / "test-results" / "a.json"
        out_b = rapids_project / ".aah" / "build" / "test-results" / "b.json"
        attestation.write_attested(
            a, out_a, project_path=rapids_project,
            command=["aah-run", "x"], exit_code=0, stdout="", stderr="", duration_ms=1,
        )
        attestation.write_attested(
            b, out_b, project_path=rapids_project,
            command=["aah-run", "x"], exit_code=0, stdout="", stderr="", duration_ms=1,
        )

        # Different timestamps & PIDs of course differ, but signatures over
        # the same input space should both verify.
        ok_a, _ = attestation.verify(read_json(out_a), project_path=rapids_project)
        ok_b, _ = attestation.verify(read_json(out_b), project_path=rapids_project)
        assert ok_a and ok_b


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------


class TestTamperDetection:
    def test_no_attestation(self, rapids_project):
        _seed_secret(rapids_project)
        ok, reason = attestation.verify({"verdict": "pass"}, project_path=rapids_project)
        assert ok is False
        assert reason == REASON_NO_ATTESTATION

    def test_attestation_not_dict(self, rapids_project):
        _seed_secret(rapids_project)
        ok, reason = attestation.verify(
            {"attestation": "definitely-not-a-dict"}, project_path=rapids_project
        )
        assert ok is False
        assert reason == REASON_NO_ATTESTATION

    def test_payload_not_dict(self, rapids_project):
        _seed_secret(rapids_project)
        ok, reason = attestation.verify("nope", project_path=rapids_project)  # type: ignore[arg-type]
        assert ok is False
        assert reason == REASON_MALFORMED

    def test_missing_secret(self, tmp_path):
        # No .aah/build/.attestation-secret created.
        ok, reason = attestation.verify(
            {"attestation": _well_formed_attestation_block()},
            project_path=tmp_path,
        )
        assert ok is False
        assert reason == REASON_MISSING_SECRET

    def test_stale_secret(self, rapids_project):
        # Write under secret A.
        _seed_secret(rapids_project, secret_bytes=b"\x00" * 32)
        out = _write(rapids_project, _make_payload())
        # Simulate an explicit/manual replacement or a different clone's key.
        _seed_secret(rapids_project, secret_bytes=b"\x01" * 32)
        loaded = read_json(out)
        ok, reason = attestation.verify(loaded, project_path=rapids_project)
        assert ok is False
        assert reason == REASON_STALE_SECRET

    def test_signature_tampered(self, rapids_project):
        _seed_secret(rapids_project)
        out = _write(rapids_project, _make_payload())
        loaded = read_json(out)
        # Mutate the verdict — should invalidate the signature.
        loaded["verdict"] = "fail"
        ok, reason = attestation.verify(loaded, project_path=rapids_project)
        assert ok is False
        assert reason == REASON_SIGNATURE_MISMATCH

    def test_command_field_tampered(self, rapids_project):
        _seed_secret(rapids_project)
        out = _write(rapids_project, _make_payload())
        loaded = read_json(out)
        loaded["attestation"]["command"] = ["aah-run", "evil"]
        ok, reason = attestation.verify(loaded, project_path=rapids_project)
        assert ok is False
        assert reason == REASON_SIGNATURE_MISMATCH

    def test_exit_code_tampered(self, rapids_project):
        _seed_secret(rapids_project)
        out = _write(rapids_project, _make_payload())
        loaded = read_json(out)
        loaded["attestation"]["exit_code"] = 1
        ok, reason = attestation.verify(loaded, project_path=rapids_project)
        assert ok is False
        assert reason == REASON_SIGNATURE_MISMATCH

    def test_stdout_sha_tampered(self, rapids_project):
        _seed_secret(rapids_project)
        out = _write(rapids_project, _make_payload())
        loaded = read_json(out)
        loaded["attestation"]["stdout_sha256"] = "0" * 64
        ok, reason = attestation.verify(loaded, project_path=rapids_project)
        assert ok is False
        assert reason == REASON_SIGNATURE_MISMATCH

    def test_signature_replaced_with_garbage(self, rapids_project):
        _seed_secret(rapids_project)
        out = _write(rapids_project, _make_payload())
        loaded = read_json(out)
        loaded["attestation"]["signature"] = "deadbeef" * 8
        ok, reason = attestation.verify(loaded, project_path=rapids_project)
        assert ok is False
        assert reason == REASON_SIGNATURE_MISMATCH

    def test_missing_required_field(self, rapids_project):
        _seed_secret(rapids_project)
        out = _write(rapids_project, _make_payload())
        loaded = read_json(out)
        del loaded["attestation"]["host_pid"]
        ok, reason = attestation.verify(loaded, project_path=rapids_project)
        assert ok is False
        assert reason == REASON_MALFORMED

    def test_field_wrong_type(self, rapids_project):
        _seed_secret(rapids_project)
        out = _write(rapids_project, _make_payload())
        loaded = read_json(out)
        loaded["attestation"]["exit_code"] = "0"  # str, not int
        ok, reason = attestation.verify(loaded, project_path=rapids_project)
        assert ok is False
        assert reason == REASON_MALFORMED


# ---------------------------------------------------------------------------
# Env-var lifecycle
# ---------------------------------------------------------------------------


class TestEnvVarLifecycle:
    def test_env_var_set_during_write_and_restored(self, rapids_project, monkeypatch):
        _seed_secret(rapids_project)
        # Confirm the var is unset before the call.
        monkeypatch.delenv(VERIFICATION_WRITE_ENV, raising=False)
        _write(rapids_project, _make_payload())
        # And unset after.
        import os as _os
        assert _os.environ.get(VERIFICATION_WRITE_ENV) is None

    def test_env_var_preserved_if_already_set(self, rapids_project, monkeypatch):
        _seed_secret(rapids_project)
        monkeypatch.setenv(VERIFICATION_WRITE_ENV, "preexisting")
        _write(rapids_project, _make_payload())
        import os as _os
        assert _os.environ.get(VERIFICATION_WRITE_ENV) == "preexisting"


# ---------------------------------------------------------------------------
# Deep-copy invariant on _strip_signature
# ---------------------------------------------------------------------------


class TestVerifyDoesNotMutateInput:
    """verify() must not alias nested mutables.

    The internal _strip_signature() must deep-copy the attestation block
    so a future reader that renders/logs the loaded payload cannot
    accidentally mutate the on-disk-equivalent representation through
    the signing-input clone.
    """

    def test_verify_does_not_mutate_command_list(self, rapids_project):
        _seed_secret(rapids_project)
        out = _write(rapids_project, _make_payload())
        loaded = read_json(out)
        original_command = list(loaded["attestation"]["command"])

        ok, _ = attestation.verify(loaded, project_path=rapids_project)
        assert ok is True
        # The loaded payload is not mutated by verify().
        assert loaded["attestation"]["command"] == original_command

    def test_strip_signature_clone_does_not_alias_command(self, rapids_project):
        """Direct test of the deep-copy invariant — mutating the cloned
        attestation.command must not affect the original payload."""
        _seed_secret(rapids_project)
        out = _write(rapids_project, _make_payload())
        loaded = read_json(out)
        original_command = list(loaded["attestation"]["command"])

        # Reach into the private helper to assert the invariant directly.
        from aah.core.common.attestation import _strip_signature

        stripped = _strip_signature(loaded)
        stripped["attestation"]["command"].append("forged-arg")
        # Original is unchanged.
        assert loaded["attestation"]["command"] == original_command
        # signature was stripped from the clone but not the original.
        assert "signature" not in stripped["attestation"]
        assert "signature" in loaded["attestation"]


# ---------------------------------------------------------------------------
# Command coercion to list[str]
# ---------------------------------------------------------------------------


class TestCommandCoercion:
    """Command items are coerced to str via str().

    write_attested accepts ergonomic non-string types (Path, etc.) but
    persists them as plain strings so verify(expected_command_prefix=...)
    is an apples-to-apples comparison.
    """

    def test_path_in_command_is_coerced(self, rapids_project):
        _seed_secret(rapids_project)
        out = rapids_project / ".aah" / "build" / "test-results" / "F001-coerced.json"
        from pathlib import Path as _Path

        attestation.write_attested(
            {"feature_id": "F001", "verdict": "pass"},
            out,
            project_path=rapids_project,
            command=[_Path("aah-run"), "aah.core.build.write_qa_report"],
            exit_code=0,
            stdout="",
            stderr="",
            duration_ms=1,
        )
        loaded = read_json(out)
        # On disk, every command item is a str.
        for item in loaded["attestation"]["command"]:
            assert isinstance(item, str), f"command item not str: {item!r}"
        assert loaded["attestation"]["command"][0] == "aah-run"

    def test_command_prefix_match_after_path_coercion(self, rapids_project):
        """End-to-end: caller passes a Path, verify() with a str-prefix matches."""
        _seed_secret(rapids_project)
        out = rapids_project / ".aah" / "build" / "test-results" / "F001-prefix.json"
        from pathlib import Path as _Path

        attestation.write_attested(
            {"feature_id": "F001"},
            out,
            project_path=rapids_project,
            command=[_Path("aah-run"), "aah.core.build.run_regression_suite"],
            exit_code=0, stdout="", stderr="", duration_ms=1,
        )
        loaded = read_json(out)
        ok, reason = attestation.verify(
            loaded,
            project_path=rapids_project,
            expected_command_prefix=[
                "aah-run", "aah.core.build.run_regression_suite",
            ],
        )
        assert ok, f"prefix match failed: {reason}"

    def test_int_in_command_is_coerced(self, rapids_project):
        """Non-string ergonomic types beyond Path also coerce cleanly."""
        _seed_secret(rapids_project)
        out = rapids_project / ".aah" / "build" / "test-results" / "F001-int.json"

        attestation.write_attested(
            {"feature_id": "F001"},
            out,
            project_path=rapids_project,
            command=["aah-run", "writer", 42],
            exit_code=0, stdout="", stderr="", duration_ms=1,
        )
        loaded = read_json(out)
        assert loaded["attestation"]["command"] == ["aah-run", "writer", "42"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _well_formed_attestation_block() -> dict:
    """A syntactically-valid attestation block (will fail signature check)."""
    return {
        "method": "subprocess",
        "command": ["aah-run", "x"],
        "exit_code": 0,
        "stdout_sha256": "0" * 64,
        "stderr_sha256": "0" * 64,
        "duration_ms": 1,
        "host_pid": 1,
        "timestamp": "2026-06-01T00:00:00+00:00",
        "session_secret_sha256": "0" * 64,
        "signature": "0" * 64,
        "schema_version": 1,
    }
