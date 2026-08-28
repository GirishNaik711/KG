#!/usr/bin/env python3
"""Audit logging for attestation decisions at git-operation boundaries."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.io_utils import read_json, write_json


def record_gate_audit(
    aah_path: Path,
    *,
    gate: str,
    artifact: str,
    verdict: str,
    reason: str,
    wave: int,
    expected_subject: dict | None = None,
    recorded_subject: dict | None = None,
) -> None:
    """Record an attestation gate audit event.

    Appends to .aah/audit/attestation-gate-log.json using the read-modify-append
    pattern (same as promote_to_develop._cleanup_feature_branches).

    Args:
        aah_path: Path to .aah/ directory
        gate: "merge" or "promote"
        artifact: "runtime" or "regression"
        verdict: "ok", "reverify_required", or "refuse"
        reason: attestation.REASON_* constant or empty string
        wave: wave number
        expected_subject: expected subject dict (branch + commit_sha) if mismatch detected
        recorded_subject: recorded subject dict from evidence if mismatch detected
    """
    audit_path = aah_path / "audit" / "attestation-gate-log.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    if audit_path.exists():
        audit_data = read_json(audit_path)
    else:
        audit_data = {"events": []}

    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "gate": gate,
        "artifact": artifact,
        "verdict": verdict,
        "reason": reason,
        "wave": wave,
    }
    if expected_subject is not None:
        event["expected_subject"] = expected_subject
    if recorded_subject is not None:
        event["recorded_subject"] = recorded_subject

    audit_data["events"].append(event)
    write_json(audit_data, audit_path)
