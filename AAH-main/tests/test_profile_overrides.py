"""Functional tests for profile-lowering overrides.

NO MOCKS. Every test builds a real temp git repo + real .aah skeleton, a real
project-local attestation key, a real attested verification-governance
registry (via the governance CLI), and creates overrides only through the real
``profile_override`` CLI. Reads go through the real reader.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aah.core.build.verification_profiles import catalog_hash, profile_binding_for_feature
from aah.core.common.io_utils import write_yaml
from tests.support.aah_project import AAHProjectBuilder


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Real git repo + .aah skeleton + attestation secret."""
    builder = (
        AAHProjectBuilder.create(tmp_path)
        .manifest(project_name="override-test")
        .secret()
        .file(".gitignore", ".aah/\n.claude/\n")
        .file("README.md", "# test\n")
        .commit("init", (".gitignore", "README.md"))
    )
    return builder.path


def _configure_governance(project: Path, **owners: str) -> subprocess.CompletedProcess:
    args = [
        "run", "core.common.governance", "configure",
        "--project-path", str(project),
    ]
    for role, ident in owners.items():
        args.extend([f"--{role.replace('_', '-')}", ident])
    return AAHProjectBuilder(project).run_module("aah.cli", *args)


def _create_override(
    project: Path,
    *,
    feature_id: str = "F001",
    from_level: str = "deep",
    to_level: str = "standard",
    reviewer: str = "alice",
    rationale: str = "reviewed and safe to lower",
    expires_at: str | None = None,
    checkpoint_config_hash: str = "abc123",
    subject_sha: str = "sha0001",
) -> subprocess.CompletedProcess:
    if expires_at is None:
        expires_at = _rfc3339(datetime.now(timezone.utc) + timedelta(days=7))
    args = [
        "create",
        "--project-path", str(project),
        "--feature-id", feature_id,
        "--from-level", from_level,
        "--to-level", to_level,
        "--reviewer", reviewer,
        "--rationale", rationale,
        "--expires-at", expires_at,
        "--checkpoint-config-hash", checkpoint_config_hash,
        "--subject-sha", subject_sha,
    ]
    return AAHProjectBuilder(project).run_module(
        "aah.core.build.profile_override", *args
    )


def _read_current(project: Path, **kwargs) -> dict | None:
    from aah.core.build.profile_override import read_current_override

    return read_current_override(
        project,
        kwargs.get("feature_id", "F001"),
        subject_sha=kwargs.get("subject_sha", "sha0001"),
        checkpoint_config_hash=kwargs.get("checkpoint_config_hash", "abc123"),
    )


def test_valid_override_round_trip(project):
    """AC8: a valid official lowering override is honored only for its
    feature/SHA/config and before expiry."""
    gov = _configure_governance(project, delivery_owner="alice", qa_governance_owner="bob")
    assert gov.returncode == 0, gov.stderr

    profiles = {
        "F001": {
            "level": "deep",
            "rule_version": "1",
            "reasons": ["explicit_security_scope"],
            "required_checks": {},
        }
    }
    write_yaml(
        {"checkpoint_configuration": {"verification_profiles": profiles}},
        project / ".aah" / "plan" / "checkpoint-config.yaml",
    )
    legacy_hash = catalog_hash(profiles)
    result = _create_override(project, checkpoint_config_hash=legacy_hash)
    assert result.returncode == 0, result.stderr

    override = _read_current(project, checkpoint_config_hash=legacy_hash)
    assert override is not None
    assert override["from_level"] == "deep"
    assert override["to_level"] == "standard"
    assert override["reviewer"] == "alice"
    binding = profile_binding_for_feature(
        project, "F001", subject_sha="sha0001"
    )
    assert binding["override_ref"] == 1

    assert _read_current(project, subject_sha="different") is None
    assert _read_current(project, checkpoint_config_hash="other") is None
    assert _read_current(
        project, feature_id="F999", checkpoint_config_hash=legacy_hash
    ) is None

    override_path = project / ".aah" / "build" / "profile-overrides" / "F001" / "override-001.json"
    assert override_path.exists()
    data = json.loads(override_path.read_text(encoding="utf-8"))
    assert data["attestation"]["signature"]
    assert data["attestation"]["command"][:4] == [
        "aah", "run", "core.build.profile_override", "create",
    ]


def test_invalid_overrides_fail_closed(project):
    """AC9: malformed, tampered, expired, unauthorized, wrong-feature,
    wrong-SHA, and wrong-config overrides all reject without lowering."""
    _configure_governance(project, delivery_owner="alice", qa_governance_owner="bob")

    overrides_dir = project / ".aah" / "build" / "profile-overrides" / "F001"

    unauth = _create_override(project, reviewer="mallory")
    assert unauth.returncode == 2, unauth.stdout
    assert not overrides_dir.exists() or not list(overrides_dir.glob("override-*.json"))

    ok = _create_override(project)
    assert ok.returncode == 0, ok.stderr
    override_path = overrides_dir / "override-001.json"

    data = json.loads(override_path.read_text(encoding="utf-8"))
    data["to_level"] = "standard"
    data["subject_sha"] = "TAMPERED"  # mutate a signed field
    override_path.write_text(json.dumps(data), encoding="utf-8")
    assert _read_current(project) is None

    past = _rfc3339(datetime.now(timezone.utc) - timedelta(days=1))
    expired = _create_override(project, feature_id="F002", expires_at=past)
    assert expired.returncode == 2, expired.stdout


def test_override_governance_limits(project):
    """AC10: the override CLI rejects lifetimes over 30 days and empty
    identity/rationale."""
    _configure_governance(project, delivery_owner="alice", qa_governance_owner="bob")

    too_long = _rfc3339(datetime.now(timezone.utc) + timedelta(days=45))
    result = _create_override(project, expires_at=too_long)
    assert result.returncode == 2
    assert "30-day" in result.stderr or "lifetime" in result.stderr

    empty_rat = _create_override(project, rationale="   ")
    assert empty_rat.returncode == 2
    assert "rationale" in empty_rat.stderr

    raise_txn = _create_override(project, from_level="standard", to_level="deep")
    assert raise_txn.returncode == 2
    assert "transition" in raise_txn.stderr

    reserved = _create_override(project, reviewer="qa")
    assert reserved.returncode == 2


def test_governance_owner_registry_fail_closed(project):
    """AC11: only identities in the attested governance registry can approve;
    a missing/tampered registry is no_signal."""
    no_reg = _create_override(project)
    assert no_reg.returncode == 2
    assert "governance" in no_reg.stderr.lower()

    _configure_governance(project, delivery_owner="alice", qa_governance_owner="bob")
    ok = _create_override(project)
    assert ok.returncode == 0, ok.stderr
    assert _read_current(project) is not None

    gov_path = project / ".aah" / "plan" / "verification-governance.yaml"
    gov_data = gov_path.read_text(encoding="utf-8")
    gov_path.write_text(gov_data.replace("alice", "eve"), encoding="utf-8")
    assert _read_current(project) is None
