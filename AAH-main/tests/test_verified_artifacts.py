from __future__ import annotations

import secrets
from pathlib import Path

import pytest
import yaml

from aah.core.common import attestation
from aah.core.common.attestation import SECRET_REL_PATH, write_attested
from aah.core.common.io_utils import read_json, write_json
from aah.core.common.verified_artifacts import (
    ArtifactState,
    load_attested_artifact,
)


WRITER_PREFIX = ["aah", "run", "core.build.real_writer"]

def _seed_secret(project_path: Path, value: bytes | None = None) -> None:
    secret_path = project_path / SECRET_REL_PATH
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_bytes(value if value is not None else secrets.token_bytes(32))

def _write_artifact(project_path: Path, name: str = "result.json") -> Path:
    path = project_path / ".aah" / "build" / "test-results" / name
    write_attested(
        {"status": "pass", "checks": 3},
        path,
        project_path=project_path,
        command=WRITER_PREFIX + ["--project-path", str(project_path)],
        exit_code=0,
        stdout="passed",
        stderr="",
        duration_ms=12,
        artifact_name="loader test result",
    )
    return path

@pytest.mark.parametrize(
    ("name", "artifact_format"),
    [("result.json", "json"), ("result.yaml", "yaml")],
)
def test_real_attestation_writer_round_trips_json_and_yaml(
    tmp_path: Path,
    name: str,
    artifact_format: str,
) -> None:
    _seed_secret(tmp_path)
    path = _write_artifact(tmp_path, name)
    if artifact_format == "yaml":
        writer_payload = read_json(path)
        path.write_text(yaml.safe_dump(writer_payload, sort_keys=False), encoding="utf-8")
    result = load_attested_artifact(
        path,
        tmp_path,
        [WRITER_PREFIX],
        artifact_format,  # type: ignore[arg-type]
    )
    assert result.state is ArtifactState.VERIFIED
    assert result.reason == ""
    assert result.payload is not None
    assert result.payload["status"] == "pass"
    assert result.payload["checks"] == 3

def test_any_legitimate_prefix_can_verify_and_verified_wins(tmp_path: Path) -> None:
    _seed_secret(tmp_path)
    path = _write_artifact(tmp_path)
    result = load_attested_artifact(
        path,
        tmp_path,
        [["aah", "run", "wrong.writer"], WRITER_PREFIX],
        "json",
    )
    assert result.state is ArtifactState.VERIFIED
    assert result.payload is not None
    assert result.reason == ""

def test_stale_secret_wins_over_other_prefix_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_secret(tmp_path)
    path = _write_artifact(tmp_path)
    outcomes = iter(
        [
            (False, attestation.REASON_COMMAND_MISMATCH),
            (False, attestation.REASON_STALE_SECRET),
            (False, attestation.REASON_SIGNATURE_MISMATCH),
        ]
    )
    monkeypatch.setattr(attestation, "verify", lambda *args, **kwargs: next(outcomes))
    result = load_attested_artifact(
        path,
        tmp_path,
        [["first"], ["second"], ["third"]],
        "json",
    )
    assert result.state is ArtifactState.STALE_SECRET
    assert result.reason == attestation.REASON_STALE_SECRET
    assert result.payload is None

def test_real_stale_secret_never_exposes_payload(tmp_path: Path) -> None:
    _seed_secret(tmp_path, b"a" * 32)
    path = _write_artifact(tmp_path)
    _seed_secret(tmp_path, b"b" * 32)
    result = load_attested_artifact(path, tmp_path, [WRITER_PREFIX], "json")
    assert result.state is ArtifactState.STALE_SECRET
    assert result.reason == attestation.REASON_STALE_SECRET
    assert result.payload is None

def test_tampered_artifact_is_unverified_and_never_exposes_payload(
    tmp_path: Path,
) -> None:
    _seed_secret(tmp_path)
    path = _write_artifact(tmp_path)
    tampered = read_json(path)
    assert isinstance(tampered, dict)
    tampered["status"] = "forged-pass"
    write_json(tampered, path)
    result = load_attested_artifact(path, tmp_path, [WRITER_PREFIX], "json")
    assert result.state is ArtifactState.UNVERIFIED
    assert result.reason == attestation.REASON_SIGNATURE_MISMATCH
    assert result.payload is None

def test_first_non_stale_refusal_reason_is_retained(tmp_path: Path) -> None:
    _seed_secret(tmp_path)
    path = _write_artifact(tmp_path)
    result = load_attested_artifact(
        path,
        tmp_path,
        [["first", "wrong"], ["second", "wrong"]],
        "json",
    )
    assert result.state is ArtifactState.UNVERIFIED
    assert result.reason == attestation.REASON_COMMAND_MISMATCH
    assert result.payload is None

def test_missing_and_malformed_files_keep_compatibility_reasons(tmp_path: Path) -> None:
    missing = load_attested_artifact(
        tmp_path / "missing.json", tmp_path, [WRITER_PREFIX], "json"
    )
    assert (missing.state, missing.reason, missing.payload) == (
        ArtifactState.MISSING,
        "missing_file",
        None,
    )
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{definitely not json", encoding="utf-8")
    unparseable = load_attested_artifact(
        invalid, tmp_path, [WRITER_PREFIX], "json"
    )
    assert (unparseable.state, unparseable.reason, unparseable.payload) == (
        ArtifactState.MALFORMED,
        "unparseable_file",
        None,
    )
    non_mapping = tmp_path / "list.json"
    non_mapping.write_text("[]", encoding="utf-8")
    malformed = load_attested_artifact(
        non_mapping, tmp_path, [WRITER_PREFIX], "json"
    )
    assert (malformed.state, malformed.reason, malformed.payload) == (
        ArtifactState.MALFORMED,
        "malformed_file",
        None,
    )
    legacy_yaml = tmp_path / "legacy-list.yaml"
    legacy_yaml.write_text("- parseable\n- nonmapping\n", encoding="utf-8")
    legacy = load_attested_artifact(
        legacy_yaml, tmp_path, [WRITER_PREFIX], "yaml"
    )
    assert (legacy.state, legacy.reason) == (
        ArtifactState.UNVERIFIED,
        attestation.REASON_NO_ATTESTATION,
    )

def test_malformed_attestation_fails_closed_instead_of_raising(tmp_path: Path) -> None:
    _seed_secret(tmp_path)
    path = _write_artifact(tmp_path)
    payload = read_json(path)
    assert isinstance(payload, dict)
    payload["attestation"]["signature"] = "not-ascii-é"
    write_json(payload, path)
    result = load_attested_artifact(path, tmp_path, [WRITER_PREFIX], "json")
    assert result.state is ArtifactState.UNVERIFIED
    assert result.reason == attestation.REASON_MALFORMED
    assert result.payload is None

def test_unsupported_format_is_a_caller_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported artifact format"):
        load_attested_artifact(
            tmp_path / "result.toml",
            tmp_path,
            [WRITER_PREFIX],
            "toml",  # type: ignore[arg-type]
        )
