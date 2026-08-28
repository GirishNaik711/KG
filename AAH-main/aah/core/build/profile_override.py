#!/usr/bin/env python3
"""Authoritative, attested verification-profile lowering overrides.

This module is the SOLE authoritative writer/reader for profile-lowering
overrides. A lowering override is the only artifact that can reduce a
feature's verification strictness (``deep`` → ``standard``); it is never
inferred from a QA verdict or a human QA-approval record.

Design posture — FAIL CLOSED:
  * Only ``deep`` → ``standard`` is an allowed transition. Raising strictness
    is an inline deterministic input (see determine_checkpoints) and is never
    expressed through this module.
  * Overrides are append-only, one file per allocation, claimed atomically via
    ``O_CREAT|O_EXCL`` under ``.aah/build/profile-overrides/FXXX/``.
  * Every override is signed with the per-session attestation secret. Reads
    verify the signature under this module's canonical command prefix; an
    unverifiable, tampered, expired, wrong-feature, wrong-SHA, or wrong-config
    override is skipped (treated as no_signal — never silently honored).
  * The reviewer identity must be an authorized owner in the attested
    verification-governance registry and must never be an implementer or QA
    actor. A missing/unverifiable registry is no_signal.
  * Maximum lifetime is 30 days, or until the checkpoint-config hash changes,
    whichever comes first.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from aah.core.common.attestation import write_attested
from aah.core.common.governance import read_governance
from aah.core.common.sequenced_store import claim_sequenced_path, sequenced_paths
from aah.core.common.time_utils import parse_rfc3339, utc_now
from aah.core.common.verified_artifacts import ArtifactState, load_attested_artifact


# Canonical command prefix recorded in the attestation block. Reads pass this
# as expected_command_prefix so a file written by any other writer fails the
# command-prefix check and is skipped.
CANONICAL_COMMAND = ["aah", "run", "core.build.profile_override", "create"]

# The only permitted (from_level, to_level) transition. Raising strictness is
# an inline deterministic input, not an override.
ALLOWED_TRANSITIONS = {("deep", "standard")}

# Maximum override lifetime. An override that claims a longer window is invalid.
MAX_LIFETIME_DAYS = 30

# Governance roles required for a lowering to be authorized.
REQUIRED_GOVERNANCE_ROLES = ("delivery_owner", "qa_governance_owner")

# Actor identities that can NEVER be an authorized lowering reviewer, even if
# they somehow appear in the registry: the implementer and the QA evaluator
# actors are the parties whose work is being reviewed.
RESERVED_ACTOR_IDENTITIES = frozenset(
    {"qa", "implementer", "aah-feature-implementer", "aah-qa-evaluator"}
)

class OverrideError(Exception):
    """Raised on any fail-closed condition when creating an override."""


def _now() -> datetime:
    return utc_now()


def _authorized_owner_identities(payload: dict) -> set[str]:
    """Return the set of non-empty owner identities from a governance payload."""
    owners = payload.get("owners")
    if not isinstance(owners, dict):
        return set()
    return {
        str(identity).strip()
        for identity in owners.values()
        if isinstance(identity, str) and identity.strip()
    }


def _overrides_dir(project_path: Path, feature_id: str) -> Path:
    return project_path / ".aah" / "build" / "profile-overrides" / feature_id


def _allocate_override(project_path: Path, feature_id: str) -> tuple[int, Path]:
    """Allocate the next gap-free override number via O_CREAT|O_EXCL.

    Mirrors qa_evidence.allocate_attempt: append-only, race-safe, never
    overwrites an existing override file.
    """
    try:
        return claim_sequenced_path(
            _overrides_dir(project_path, feature_id), "override-"
        )
    except RuntimeError as exc:
        raise OverrideError(
            f"Failed to allocate override for {feature_id} after 100 retries"
        ) from exc


def create_override(
    project_path: Path,
    *,
    feature_id: str,
    from_level: str,
    to_level: str,
    reviewer: str,
    rationale: str,
    expires_at: str,
    checkpoint_config_hash: str,
    subject_sha: str,
    argv: list[str] | None = None,
) -> dict:
    """Create an attested, append-only lowering override.

    Raises OverrideError on any invalid input, disallowed transition, empty
    identity/rationale, bad expiry, over-limit lifetime, or when the governance
    registry does not authorize the reviewer. Returns the written payload.
    """
    if not isinstance(feature_id, str) or not feature_id.strip():
        raise OverrideError("feature_id is required")
    if (from_level, to_level) not in ALLOWED_TRANSITIONS:
        raise OverrideError(
            f"disallowed transition {from_level!r} -> {to_level!r}; "
            f"only {sorted(ALLOWED_TRANSITIONS)} permitted"
        )
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise OverrideError("reviewer identity is required (non-empty)")
    if not isinstance(rationale, str) or not rationale.strip():
        raise OverrideError("rationale is required (non-empty)")
    if not isinstance(checkpoint_config_hash, str) or not checkpoint_config_hash.strip():
        raise OverrideError("checkpoint_config_hash is required")
    if not isinstance(subject_sha, str) or not subject_sha.strip():
        raise OverrideError("subject_sha is required")

    reviewer = reviewer.strip()
    if reviewer.lower() in RESERVED_ACTOR_IDENTITIES:
        raise OverrideError(
            f"reviewer {reviewer!r} is a reserved implementer/QA actor and cannot "
            "authorize a lowering"
        )

    expires_dt = parse_rfc3339(expires_at)
    if expires_dt is None:
        raise OverrideError(f"expires_at must be an RFC3339 timestamp, got {expires_at!r}")

    created_dt = _now()
    if expires_dt <= created_dt:
        raise OverrideError("expires_at must be in the future")
    if expires_dt - created_dt > timedelta(days=MAX_LIFETIME_DAYS):
        raise OverrideError(
            f"override lifetime exceeds the {MAX_LIFETIME_DAYS}-day maximum"
        )

    # Governance authorization — the registry must verify and name the reviewer
    # as an authorized owner. A missing/unverifiable registry is no_signal.
    payload_gov, reason = read_governance(
        project_path, required_roles=REQUIRED_GOVERNANCE_ROLES
    )
    if payload_gov is None:
        raise OverrideError(f"governance registry unavailable: {reason}")
    authorized = _authorized_owner_identities(payload_gov)
    if reviewer not in authorized:
        raise OverrideError(
            f"reviewer {reviewer!r} is not an authorized owner in the "
            "verification-governance registry"
        )

    payload = {
        "schema_version": 1,
        "feature_id": feature_id,
        "from_level": from_level,
        "to_level": to_level,
        "reviewer": reviewer,
        "rationale": rationale.strip(),
        "created_at": created_dt.isoformat(),
        "expires_at": expires_dt.isoformat(),
        "checkpoint_config_hash": checkpoint_config_hash.strip(),
        "subject_sha": subject_sha.strip(),
    }

    override_num, override_path = _allocate_override(project_path, feature_id)
    payload["override"] = override_num

    command = list(CANONICAL_COMMAND) + list(argv or [])
    write_attested(
        payload,
        override_path,
        project_path=project_path,
        command=command,
        exit_code=0,
        stdout="verification profile lowering override created",
        stderr="",
        duration_ms=0,
        artifact_name=f"{feature_id} profile lowering override {override_num}",
    )
    return payload


def _verified_overrides(project_path: Path, feature_id: str) -> list[dict]:
    """Return all attestation-verified override payloads, sorted ascending."""
    overrides_dir = _overrides_dir(project_path, feature_id)
    if not overrides_dir.exists():
        return []
    verified: list[dict] = []
    for override_num, candidate in sequenced_paths(overrides_dir, "override-"):
        artifact = load_attested_artifact(
            candidate, project_path, CANONICAL_COMMAND, "json"
        )
        if artifact.state is not ArtifactState.VERIFIED:
            continue
        data = artifact.payload
        assert data is not None
        data["override"] = override_num
        verified.append(data)
    verified.sort(key=lambda item: item["override"])
    return verified


def read_current_override(
    project_path: Path,
    feature_id: str,
    *,
    subject_sha: str,
    checkpoint_config_hash: str,
) -> dict | None:
    """Return the latest valid lowering override for the exact binding, else None.

    Fail-closed: any override that fails attestation, targets a different
    feature/SHA/config, declares a disallowed transition, has an empty
    rationale, is missing its creation time, is expired, exceeds the lifetime
    cap, or whose reviewer is no longer an authorized owner is skipped. The
    absence of a valid override is no_signal (None), never a lowering.
    """
    if not isinstance(subject_sha, str) or not subject_sha.strip():
        return None
    if not isinstance(checkpoint_config_hash, str) or not checkpoint_config_hash.strip():
        return None

    subject_sha = subject_sha.strip()
    checkpoint_config_hash = checkpoint_config_hash.strip()

    payload_gov, _reason = read_governance(
        project_path, required_roles=REQUIRED_GOVERNANCE_ROLES
    )
    authorized = _authorized_owner_identities(payload_gov) if payload_gov else set()

    now = _now()

    # Latest valid override wins — iterate newest first and return the first
    # that satisfies every binding.
    for override in reversed(_verified_overrides(project_path, feature_id)):
        if override.get("feature_id") != feature_id:
            continue
        if override.get("subject_sha") != subject_sha:
            continue
        if override.get("checkpoint_config_hash") != checkpoint_config_hash:
            continue
        transition = (override.get("from_level"), override.get("to_level"))
        if transition not in ALLOWED_TRANSITIONS:
            continue
        rationale = override.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            continue
        reviewer = override.get("reviewer")
        if not isinstance(reviewer, str) or not reviewer.strip():
            continue
        # Governance registry unavailable OR reviewer no longer an owner → skip.
        if not authorized or reviewer.strip() not in authorized:
            continue
        if reviewer.strip().lower() in RESERVED_ACTOR_IDENTITIES:
            continue
        created_dt = parse_rfc3339(override.get("created_at", ""))
        expires_dt = parse_rfc3339(override.get("expires_at", ""))
        if created_dt is None or expires_dt is None:
            continue
        if expires_dt <= now:
            continue  # expired
        if expires_dt - created_dt > timedelta(days=MAX_LIFETIME_DAYS):
            continue  # over-limit lifetime
        return override
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verification profile lowering overrides"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create_p = sub.add_parser("create", help="Create an attested lowering override")
    create_p.add_argument("--project-path", type=Path, default=None)
    create_p.add_argument("--feature-id", required=True)
    create_p.add_argument("--from-level", required=True)
    create_p.add_argument("--to-level", required=True)
    create_p.add_argument("--reviewer", required=True)
    create_p.add_argument("--rationale", required=True)
    create_p.add_argument("--expires-at", required=True)
    create_p.add_argument("--checkpoint-config-hash", required=True)
    create_p.add_argument("--subject-sha", required=True)

    read_p = sub.add_parser("read-current", help="Read the current valid override")
    read_p.add_argument("--project-path", type=Path, default=None)
    read_p.add_argument("--feature-id", required=True)
    read_p.add_argument("--subject-sha", required=True)
    read_p.add_argument("--checkpoint-config-hash", required=True)

    args = parser.parse_args()

    from aah.core.common.config import require_project_path

    project_path = require_project_path(getattr(args, "project_path", None))

    if args.command == "create":
        try:
            payload = create_override(
                project_path,
                feature_id=args.feature_id,
                from_level=args.from_level,
                to_level=args.to_level,
                reviewer=args.reviewer,
                rationale=args.rationale,
                expires_at=args.expires_at,
                checkpoint_config_hash=args.checkpoint_config_hash,
                subject_sha=args.subject_sha,
                argv=sys.argv[1:],
            )
        except OverrideError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(2)
        json.dump(payload, sys.stdout, indent=2)
        print()
        sys.exit(0)

    elif args.command == "read-current":
        override = read_current_override(
            project_path,
            args.feature_id,
            subject_sha=args.subject_sha,
            checkpoint_config_hash=args.checkpoint_config_hash,
        )
        if override is None:
            print("no_signal: no valid override", file=sys.stderr)
            json.dump({"override": None}, sys.stdout, indent=2)
            print()
            sys.exit(1)
        json.dump(override, sys.stdout, indent=2)
        print()
        sys.exit(0)


if __name__ == "__main__":
    main()
