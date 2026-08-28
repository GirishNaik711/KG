"""Functional governance-registry writer/reader tests."""

import secrets

import pytest

from aah.core.common.governance import configure_governance, read_governance
from aah.core.common.io_utils import read_yaml, write_yaml
from tests.support.aah_project import AAHProjectBuilder


def test_governance_registry_round_trip_and_tamper(tmp_path):
    AAHProjectBuilder(tmp_path).secret()
    payload = configure_governance(
        tmp_path,
        {"delivery_owner": "delivery@example", "qa_governance_owner": "qa@example"},
    )
    assert payload["owners"]["delivery_owner"] == "delivery@example"
    loaded, reason = read_governance(tmp_path)
    assert reason == ""
    assert loaded["owners"]["qa_governance_owner"] == "qa@example"

    path = tmp_path / ".aah" / "plan" / "verification-governance.yaml"
    tampered = read_yaml(path)
    tampered["owners"]["delivery_owner"] = "attacker"
    write_yaml(tampered, path)
    loaded, reason = read_governance(tmp_path)
    assert loaded is None
    assert reason == "signature_mismatch"


def test_governance_survives_restart_and_rejects_explicit_key_mismatch(tmp_path):
    project = AAHProjectBuilder(tmp_path).secret()
    with pytest.raises(ValueError):
        configure_governance(tmp_path, {"delivery_owner": "delivery@example"})

    project.manifest(project_name="p")
    result = project.run_module(
        "aah.core.common.governance", "configure", "--project-path", str(tmp_path),
        "--delivery-owner", "delivery@example",
        "--qa-governance-owner", "qa@example",
    )
    assert result.returncode == 0, result.stderr
    payload, reason = read_governance(tmp_path)
    assert payload is not None and reason == ""

    project.secret()
    payload, reason = read_governance(tmp_path)
    assert payload is not None and reason == ""

    (tmp_path / ".aah" / "build" / ".attestation-secret").write_bytes(
        secrets.token_bytes(32)
    )
    payload, reason = read_governance(tmp_path)
    assert payload is None
    assert reason == "stale_secret"
