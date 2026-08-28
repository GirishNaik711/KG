#!/usr/bin/env python3
"""Runtime-profile proposal / confirmation / cloud-probe authorization writer.

A resolved runtime profile is only ever a *proposal*. Under
``runtime_profile_v1: true`` only an attested, hash-bound confirmation created
by this CLI may enter runtime verification; an inferred proposal (and a
``report_only`` audit event) can never satisfy an enforced gate.

Subcommands:
  propose               Resolve the profile from confirmed decisions + validated
                        cloud-readiness and write the (un-attested) proposal +
                        derived runtime-profile.yaml.
  confirm               Governed, attested confirmation binding the profile /
                        decision / readiness / project-anchor hashes.
  authorize-cloud-probes  Governed, attested authorization gate that must exist
                        before ``runtime_cloud_probes_v1`` may become true.
  read-confirmed        Verify the attested confirmation and re-derive every
                        hash; any drift returns ``no_signal`` (never mutates).

Stdlib + attestation + governance + readiness + io_utils only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aah.core.common import readiness as readiness_mod
from aah.core.common.attestation import write_attested
from aah.core.common.governance import read_governance
from aah.core.common.io_utils import read_yaml, write_yaml
from aah.core.common.manifest import rollout_value
from aah.core.common.readiness import (
    CLOUD_TOPOLOGIES,
    load_runtime_resources,
    resolve_runtime_profile,
)
from aah.core.common.time_utils import rfc3339_now
from aah.core.common.verified_artifacts import ArtifactState, load_attested_artifact

CONFIRM_COMMAND = ["aah", "run", "core.common.runtime_profile", "confirm"]
AUTHORIZE_COMMAND = ["aah", "run", "core.common.runtime_profile", "authorize-cloud-probes"]

PROPOSAL_REL = Path("plan") / "runtime-profile-proposal.yaml"
PROFILE_REL = Path("plan") / "runtime-profile.yaml"
CONFIRMATION_REL = Path("plan") / "runtime-profile-confirmation.json"
CLOUD_AUTH_REL = Path("plan") / "runtime-cloud-probe-authorization.json"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _registry_context(aah_path: Path) -> dict:
    # A missing decision-registry is not fatal: runtime intent may live in the
    # discuss slug registry, which resolve_runtime_profile falls back to. Return
    # an empty context so that fallback can run.
    registry_path = aah_path / "decision-registry.yaml"
    if not registry_path.exists():
        return {}
    registry = read_yaml(registry_path)
    return registry.get("context", {}) if isinstance(registry, dict) else {}


def _anchor_hash(profile: dict) -> str:
    return readiness_mod._canonical_hash(profile.get("project_anchor") or {})


def _rollout_state(aah_path: Path) -> object:
    """Return features.runtime_profile_v1 (default report_only)."""
    return rollout_value(aah_path, "runtime_profile_v1")


def _resolve_current_profile(aah_path: Path) -> dict:
    context = _registry_context(aah_path)
    resources = load_runtime_resources(aah_path)
    return resolve_runtime_profile(aah_path, context, resources)


# ---------------------------------------------------------------------------
# propose
# ---------------------------------------------------------------------------


def propose(project_path: Path) -> dict:
    """Resolve and write the runtime-profile proposal (NOT attested)."""
    aah_path = project_path / ".aah"
    profile = _resolve_current_profile(aah_path)

    now = rfc3339_now()
    proposal = {
        # Hashed profile block (byte-stable across identical inputs).
        "profile": profile,
        # Fields OUTSIDE the hashed block.
        "confirmation_status": "proposed",
        "profile_hash": profile["profile_hash"],
        "decision_hash": profile["decision_hash"],
        "readiness_hash": profile["readiness_hash"],
        "project_anchor_hash": _anchor_hash(profile),
        "deployment_topology": profile["deployment_topology"],
        "source_decisions": profile["source_decisions"],
        "provider_anchor": profile["provider_anchor"],
        "project_anchor": profile["project_anchor"],
        "rationale": (
            "Runtime profile inferred from the discuss runtime decision "
            "('development-methodology') and validated cloud-readiness. Proposal "
            "only; requires governed confirmation before it can satisfy an "
            "enforced runtime gate."
        ),
        "generated_at": now,
    }

    write_yaml(proposal, aah_path / PROPOSAL_REL)
    write_yaml(proposal, aah_path / PROFILE_REL)

    rollout = _rollout_state(aah_path)
    if rollout == "report_only":
        print(
            "AUDIT runtime_profile_v1: runtime profile proposed for comparison "
            "only; it does not satisfy an enforced gate until governed confirmation",
            file=sys.stderr,
        )
    return proposal


# ---------------------------------------------------------------------------
# confirm
# ---------------------------------------------------------------------------


def confirm(
    project_path: Path,
    *,
    reviewer: str,
    security_reviewer: str,
    rationale: str,
    profile_hash: str,
    cloud_reviewer: str | None = None,
) -> tuple[dict | None, str]:
    """Write the attested confirmation. Returns ``(payload, "")`` or ``(None, reason)``."""
    aah_path = project_path / ".aah"

    proposal_path = aah_path / PROPOSAL_REL
    if not proposal_path.exists():
        return None, "missing_proposal"
    proposal = read_yaml(proposal_path)
    profile = proposal.get("profile") if isinstance(proposal, dict) else None
    if not isinstance(profile, dict):
        return None, "malformed_proposal"

    # Recompute the profile hash from the on-disk proposal and fail closed on
    # any mismatch with the reviewer-supplied --profile-hash.
    recomputed = readiness_mod._profile_hash(profile)
    if profile.get("profile_hash") != recomputed:
        return None, "proposal_hash_tampered"
    if profile_hash != recomputed:
        return None, "profile_hash_mismatch"

    topology = str(profile.get("deployment_topology") or "")
    is_cloud = topology in CLOUD_TOPOLOGIES

    required_roles = ["delivery_owner", "security_owner"]
    if is_cloud:
        required_roles.append("cloud_platform_owner")
    governance, reason = read_governance(project_path, required_roles=tuple(required_roles))
    if governance is None:
        return None, reason
    owners = governance["owners"]

    if reviewer.strip() != owners.get("delivery_owner", "").strip():
        return None, "reviewer_not_delivery_owner"
    if security_reviewer.strip() != owners.get("security_owner", "").strip():
        return None, "security_reviewer_not_security_owner"
    if is_cloud:
        if not cloud_reviewer or cloud_reviewer.strip() != owners.get("cloud_platform_owner", "").strip():
            return None, "cloud_reviewer_not_cloud_platform_owner"

    payload = {
        "confirmation_status": "confirmed",
        "profile_hash": recomputed,
        "decision_hash": profile.get("decision_hash"),
        "readiness_hash": profile.get("readiness_hash"),
        "project_anchor_hash": _anchor_hash(profile),
        "deployment_topology": topology,
        "reviewer": reviewer.strip(),
        "security_reviewer": security_reviewer.strip(),
        "rationale": rationale,
        "confirmed_at": rfc3339_now(),
    }
    if is_cloud:
        payload["cloud_reviewer"] = cloud_reviewer.strip()

    write_attested(
        payload,
        aah_path / CONFIRMATION_REL,
        project_path=project_path,
        command=CONFIRM_COMMAND,
        exit_code=0,
        stdout="runtime profile confirmed",
        stderr="",
        duration_ms=0,
        artifact_name="runtime profile confirmation",
    )
    return payload, ""


# ---------------------------------------------------------------------------
# authorize-cloud-probes
# ---------------------------------------------------------------------------


def authorize_cloud_probes(
    project_path: Path,
    *,
    environment: str,
    credential_scope: str,
    rate_limit: int,
    cost_cap: float,
    cloud_reviewer: str,
    cost_reviewer: str,
    rationale: str,
) -> tuple[dict | None, str]:
    """Write the attested cloud-probe authorization. Returns ``(payload, "")`` or ``(None, reason)``."""
    aah_path = project_path / ".aah"

    if not str(environment).strip():
        return None, "missing_environment"
    if not str(credential_scope).strip():
        return None, "missing_credential_scope"
    if not (isinstance(rate_limit, int) and rate_limit > 0):
        return None, "invalid_rate_limit"
    if not (isinstance(cost_cap, (int, float)) and cost_cap > 0):
        return None, "invalid_cost_cap"
    if not str(rationale).strip():
        return None, "missing_rationale"

    # Bind to the current profile + anchor so a changed profile invalidates it.
    try:
        profile = _resolve_current_profile(aah_path)
    except ValueError:
        return None, "profile_unresolved"

    governance, reason = read_governance(
        project_path, required_roles=("cloud_platform_owner", "cost_owner")
    )
    if governance is None:
        return None, reason
    owners = governance["owners"]
    if cloud_reviewer.strip() != owners.get("cloud_platform_owner", "").strip():
        return None, "cloud_reviewer_not_cloud_platform_owner"
    if cost_reviewer.strip() != owners.get("cost_owner", "").strip():
        return None, "cost_reviewer_not_cost_owner"

    payload = {
        "authorization_status": "authorized",
        "environment": str(environment).strip(),
        "credential_scope": str(credential_scope).strip(),
        "rate_limit": int(rate_limit),
        "cost_cap": float(cost_cap),
        "profile_hash": profile["profile_hash"],
        "project_anchor_hash": _anchor_hash(profile),
        "cloud_reviewer": cloud_reviewer.strip(),
        "cost_reviewer": cost_reviewer.strip(),
        "rationale": rationale,
        "authorized_at": rfc3339_now(),
    }
    write_attested(
        payload,
        aah_path / CLOUD_AUTH_REL,
        project_path=project_path,
        command=AUTHORIZE_COMMAND,
        exit_code=0,
        stdout="cloud probes authorized",
        stderr="",
        duration_ms=0,
        artifact_name="runtime cloud probe authorization",
    )
    return payload, ""


# ---------------------------------------------------------------------------
# read-confirmed
# ---------------------------------------------------------------------------


def read_confirmed(project_path: Path) -> dict:
    """Verify the attested confirmation and re-derive every bound hash.

    Never mutates or re-signs. Returns a result dict with ``result`` being
    ``confirmed`` only when the attestation verifies AND all bound hashes match
    the freshly re-derived current profile; otherwise ``no_signal`` with a
    ``reason``.
    """
    aah_path = project_path / ".aah"
    path = aah_path / CONFIRMATION_REL
    artifact = load_attested_artifact(
        path, project_path, CONFIRM_COMMAND, "json"
    )
    if artifact.state is ArtifactState.MISSING:
        return {"result": "no_signal", "reason": "missing_confirmation"}
    if artifact.state is ArtifactState.MALFORMED:
        return {"result": "no_signal", "reason": "malformed_confirmation"}
    if artifact.state is not ArtifactState.VERIFIED:
        return {"result": "no_signal", "reason": artifact.reason}
    payload = artifact.payload
    assert payload is not None

    # Re-derive the current profile and compare every bound hash.
    try:
        current = _resolve_current_profile(aah_path)
    except ValueError:
        return {"result": "no_signal", "reason": "profile_unresolved"}

    bound = {
        "profile_hash": payload.get("profile_hash"),
        "decision_hash": payload.get("decision_hash"),
        "readiness_hash": payload.get("readiness_hash"),
        "project_anchor_hash": payload.get("project_anchor_hash"),
    }
    fresh = {
        "profile_hash": current["profile_hash"],
        "decision_hash": current["decision_hash"],
        "readiness_hash": current["readiness_hash"],
        "project_anchor_hash": _anchor_hash(current),
    }
    for key, expected in bound.items():
        if expected != fresh[key]:
            return {"result": "no_signal", "reason": f"stale_hash:{key}"}

    return {
        "result": "confirmed",
        "profile_hash": fresh["profile_hash"],
        "deployment_topology": current["deployment_topology"],
        "reviewer": payload.get("reviewer"),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Runtime profile proposal/confirmation")
    sub = parser.add_subparsers(dest="command", required=True)

    p_propose = sub.add_parser("propose")
    p_propose.add_argument("--project-path", type=Path, default=None)

    p_confirm = sub.add_parser("confirm")
    p_confirm.add_argument("--project-path", type=Path, default=None)
    p_confirm.add_argument("--reviewer", required=True)
    p_confirm.add_argument("--security-reviewer", required=True)
    p_confirm.add_argument("--cloud-reviewer")
    p_confirm.add_argument("--rationale", required=True)
    p_confirm.add_argument("--profile-hash", required=True)

    p_auth = sub.add_parser("authorize-cloud-probes")
    p_auth.add_argument("--project-path", type=Path, default=None)
    p_auth.add_argument("--environment", required=True)
    p_auth.add_argument("--credential-scope", required=True)
    p_auth.add_argument("--rate-limit", type=int, required=True)
    p_auth.add_argument("--cost-cap", type=float, required=True)
    p_auth.add_argument("--cloud-reviewer", required=True)
    p_auth.add_argument("--cost-reviewer", required=True)
    p_auth.add_argument("--rationale", required=True)

    p_read = sub.add_parser("read-confirmed")
    p_read.add_argument("--project-path", type=Path, default=None)

    args = parser.parse_args()

    from aah.core.common.config import require_project_path

    project_path = require_project_path(getattr(args, "project_path", None))

    if args.command == "propose":
        try:
            proposal = propose(project_path)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        json.dump(proposal, sys.stdout, indent=2, default=str)
        print()
        sys.exit(0)

    if args.command == "confirm":
        payload, reason = confirm(
            project_path,
            reviewer=args.reviewer,
            security_reviewer=args.security_reviewer,
            cloud_reviewer=args.cloud_reviewer,
            rationale=args.rationale,
            profile_hash=args.profile_hash,
        )
        if payload is None:
            print(f"Error: confirmation rejected ({reason})", file=sys.stderr)
            sys.exit(1)
        json.dump(payload, sys.stdout, indent=2, default=str)
        print()
        sys.exit(0)

    if args.command == "authorize-cloud-probes":
        payload, reason = authorize_cloud_probes(
            project_path,
            environment=args.environment,
            credential_scope=args.credential_scope,
            rate_limit=args.rate_limit,
            cost_cap=args.cost_cap,
            cloud_reviewer=args.cloud_reviewer,
            cost_reviewer=args.cost_reviewer,
            rationale=args.rationale,
        )
        if payload is None:
            print(f"Error: authorization rejected ({reason})", file=sys.stderr)
            sys.exit(1)
        json.dump(payload, sys.stdout, indent=2, default=str)
        print()
        sys.exit(0)

    if args.command == "read-confirmed":
        result = read_confirmed(project_path)
        json.dump(result, sys.stdout, indent=2, default=str)
        print()
        sys.exit(0)


if __name__ == "__main__":
    main()
