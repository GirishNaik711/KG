"""Functional tests for runtime-profile proposal / confirmation / cloud-probe auth.

NO MOCKS: real temp project, real attestation secret, real registry
CLI, real cloud-readiness, real governance, and the real runtime_profile CLI
driven via subprocess.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

from tests._runtime_helpers import (
    confirm_profile_cli,
    make_project,
    proposal_profile_hash,
    read_attested_wave,
    run_module,
    run_verify_cli,
    write_cloud_readiness,
)

RP_MODULE = "aah.core.common.runtime_profile"


def _rp(project_path: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return run_module(
        RP_MODULE, *args, "--project-path", str(project_path), check=check,
    )


def _confirm_proposal(project: Path) -> str:
    assert _rp(project, "propose").returncode == 0
    profile_hash = proposal_profile_hash(project / ".aah")
    result = _rp(
        project, "confirm",
        "--reviewer", "alice", "--security-reviewer", "sec-owner",
        "--rationale", "ok", "--profile-hash", profile_hash,
    )
    assert result.returncode == 0
    return profile_hash


def _read_confirmed(project_path: Path) -> dict:
    result = _rp(project_path, "read-confirmed")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_proposed_blocks_confirmed_passes(tmp_path):
    """AC7: an inferred proposal cannot satisfy the gate; a CLI confirmation can."""
    project = make_project(tmp_path)
    aah = project / ".aah"

    assert _rp(project, "propose").returncode == 0

    result = _read_confirmed(project)
    assert result["result"] == "no_signal"
    assert result["reason"] == "missing_confirmation"

    ph = proposal_profile_hash(aah)
    confirm = _rp(
        project, "confirm",
        "--reviewer", "alice",
        "--security-reviewer", "sec-owner",
        "--rationale", "reviewed and approved",
        "--profile-hash", ph,
    )
    assert confirm.returncode == 0, confirm.stderr

    result = _read_confirmed(project)
    assert result["result"] == "confirmed"
    assert result["profile_hash"] == ph


def test_changed_inputs_invalidate_confirmation(tmp_path):
    """AC8: changing decision/readiness/profile/anchor makes confirmation stale."""
    project = make_project(tmp_path)
    aah = project / ".aah"
    _confirm_proposal(project)
    assert _read_confirmed(project)["result"] == "confirmed"

    write_cloud_readiness(aah, region="eu-west-1")
    stale = _read_confirmed(project)
    assert stale["result"] == "no_signal"
    assert stale["reason"].startswith("stale_hash:")


def test_invalid_confirmation_fails_closed(tmp_path):
    """AC9: wrong-project / malformed / tampered / hand-written confirmation blocks."""
    project = make_project(tmp_path)
    aah = project / ".aah"
    ph = _confirm_proposal(project)

    conf_path = aah / "plan" / "runtime-profile-confirmation.json"

    payload = json.loads(conf_path.read_text())
    payload["profile_hash"] = "0" * 64
    conf_path.write_text(json.dumps(payload))
    tampered = _read_confirmed(project)
    assert tampered["result"] == "no_signal"
    assert tampered["reason"] == "signature_mismatch"

    conf_path.write_text(json.dumps({"confirmation_status": "confirmed", "profile_hash": ph}))
    handwritten = _read_confirmed(project)
    assert handwritten["result"] == "no_signal"
    assert handwritten["reason"] == "no_attestation"

    conf_path.write_text("{ not json")
    malformed = _read_confirmed(project)
    assert malformed["result"] == "no_signal"
    assert malformed["reason"] == "malformed_confirmation"


def test_runtime_confirmation_requires_authorized_owner(tmp_path):
    """AC10: missing/tampered governance or unauthorized confirmer -> no_signal/error."""
    project = make_project(tmp_path)
    aah = project / ".aah"
    assert _rp(project, "propose").returncode == 0
    ph = proposal_profile_hash(aah)

    wrong = _rp(
        project, "confirm",
        "--reviewer", "mallory", "--security-reviewer", "sec-owner",
        "--rationale", "ok", "--profile-hash", ph,
    )
    assert wrong.returncode != 0
    assert "reviewer_not_delivery_owner" in wrong.stderr
    assert not (aah / "plan" / "runtime-profile-confirmation.json").exists()

    wrong_sec = _rp(
        project, "confirm",
        "--reviewer", "alice", "--security-reviewer", "not-sec",
        "--rationale", "ok", "--profile-hash", ph,
    )
    assert wrong_sec.returncode != 0
    assert "security_reviewer_not_security_owner" in wrong_sec.stderr

    gov_path = aah / "plan" / "verification-governance.yaml"
    gov = yaml.safe_load(gov_path.read_text())
    gov["owners"]["delivery_owner"] = "attacker"
    gov_path.write_text(yaml.safe_dump(gov))
    tampered = _rp(
        project, "confirm",
        "--reviewer", "attacker", "--security-reviewer", "sec-owner",
        "--rationale", "ok", "--profile-hash", ph,
    )
    assert tampered.returncode != 0
    assert not (aah / "plan" / "runtime-profile-confirmation.json").exists()


def test_cloud_probe_authorization_contract(tmp_path):
    """AC11: cloud-probe auth requires env/scope/limits + both owners; changed profile invalidates."""
    project = make_project(
        tmp_path,
        topology="local-cloud-ready",
        transport="localhost",
        cloud_platform_owner="cloud-owner",
        cost_owner="cost-owner",
    )
    aah = project / ".aah"
    assert _rp(project, "propose").returncode == 0

    def authorize(**over):
        args = {
            "--environment": "staging",
            "--credential-scope": "read-only-metrics",
            "--rate-limit": "5",
            "--cost-cap": "10.0",
            "--cloud-reviewer": "cloud-owner",
            "--cost-reviewer": "cost-owner",
            "--rationale": "bounded probe run",
        }
        args.update(over)
        flat = []
        for k, v in args.items():
            flat += [k, v]
        return _rp(project, "authorize-cloud-probes", *flat)

    ok = authorize()
    assert ok.returncode == 0, ok.stderr
    auth_path = aah / "plan" / "runtime-cloud-probe-authorization.json"
    assert auth_path.exists()

    bad_cap = authorize(**{"--cost-cap": "0"})
    assert bad_cap.returncode != 0
    assert "invalid_cost_cap" in bad_cap.stderr

    bad_owner = authorize(**{"--cost-reviewer": "nobody"})
    assert bad_owner.returncode != 0
    assert "cost_reviewer_not_cost_owner" in bad_owner.stderr

    from aah.core.common.attestation import verify
    from aah.core.common.readiness import load_runtime_resources, resolve_runtime_profile

    payload = json.loads(auth_path.read_text())
    valid, reason = verify(
        payload, project_path=project,
        expected_command_prefix=["aah", "run", "core.common.runtime_profile", "authorize-cloud-probes"],
    )
    assert valid, reason
    # Runtime intent derived from the discuss slug registry (empty context).
    current = resolve_runtime_profile(aah, {}, load_runtime_resources(aah))
    assert payload["profile_hash"] == current["profile_hash"]

    write_cloud_readiness(aah, region="eu-central-1")
    changed = resolve_runtime_profile(aah, {}, load_runtime_resources(aah))
    assert payload["profile_hash"] != changed["profile_hash"]
