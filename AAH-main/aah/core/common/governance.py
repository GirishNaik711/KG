#!/usr/bin/env python3
"""Authoritative, attested verification-governance owner registry."""

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.attestation import write_attested
from aah.core.common.time_utils import rfc3339_now
from aah.core.common.verified_artifacts import ArtifactState, load_attested_artifact


CANONICAL_COMMAND = ["aah", "run", "core.common.governance", "configure"]
REQUIRED_ROLES = ("delivery_owner", "qa_governance_owner")
OPTIONAL_ROLES = (
    "security_owner",
    "compliance_owner",
    "cloud_platform_owner",
    "cost_owner",
    "verification_architecture_owner",
)


def configure_governance(project_path: Path, owners: dict[str, str]) -> dict:
    """Write the official registry; empty identities are rejected."""
    missing = [role for role in REQUIRED_ROLES if not str(owners.get(role, "")).strip()]
    invalid = [role for role, identity in owners.items() if not str(identity).strip()]
    if missing or invalid:
        names = sorted(set(missing + invalid))
        raise ValueError(f"non-empty owner identity required: {', '.join(names)}")
    payload = {
        "schema_version": 1,
        "created_at": rfc3339_now(),
        "owners": {role: str(identity).strip() for role, identity in sorted(owners.items())},
    }
    output = project_path / ".aah" / "plan" / "verification-governance.yaml"
    command = list(CANONICAL_COMMAND)
    for role, identity in sorted(payload["owners"].items()):
        command.extend([f"--{role.replace('_', '-')}", identity])
    write_attested(
        payload,
        output,
        project_path=project_path,
        command=command,
        exit_code=0,
        stdout="verification governance configured",
        stderr="",
        duration_ms=0,
        artifact_name="verification governance registry",
    )
    return payload


def read_governance(
    project_path: Path, required_roles: tuple[str, ...] = REQUIRED_ROLES
) -> tuple[dict | None, str]:
    """Read and verify the registry, returning ``(None, reason)`` as no-signal."""
    path = project_path / ".aah" / "plan" / "verification-governance.yaml"
    artifact = load_attested_artifact(
        path, project_path, CANONICAL_COMMAND, "yaml"
    )
    if artifact.state is ArtifactState.MISSING:
        return None, "missing_governance_registry"
    if artifact.state is ArtifactState.MALFORMED:
        return None, "malformed_governance_registry"
    if artifact.state is not ArtifactState.VERIFIED:
        return None, artifact.reason
    payload = artifact.payload
    assert payload is not None
    owners = payload.get("owners")
    if not isinstance(owners, dict):
        return None, "malformed_owner_registry"
    for role in required_roles:
        if not isinstance(owners.get(role), str) or not owners[role].strip():
            return None, f"missing_owner:{role}"
    return payload, ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure verification governance owners")
    sub = parser.add_subparsers(dest="command", required=True)
    configure = sub.add_parser("configure")
    configure.add_argument("--project-path", type=Path, default=None)
    for role in REQUIRED_ROLES:
        configure.add_argument(f"--{role.replace('_', '-')}", required=True)
    for role in OPTIONAL_ROLES:
        configure.add_argument(f"--{role.replace('_', '-')}")
    args = parser.parse_args()

    from aah.core.common.config import require_project_path

    project_path = require_project_path(args.project_path)
    owners = {
        role: getattr(args, role)
        for role in REQUIRED_ROLES + OPTIONAL_ROLES
        if getattr(args, role, None) is not None
    }
    try:
        payload = configure_governance(project_path, owners)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    json.dump(payload, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
