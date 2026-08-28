#!/usr/bin/env python3
"""QA evidence management — append-only attempt history with idempotent derivation.

Sequential, race-safe, gap-free attempt allocation with subject-bound evidence.
Every QA run writes a new attempt-NNN.json and matching test evidence; neither
is overwritten.

On-disk layout:
  .aah/build/qa-results/FXXX/attempt-NNN.json          # authoritative, attested
  .aah/build/qa-results/FXXX/attempt-NNN-tests.json    # QA rerun evidence, attested, subject-bound

Where NNN is a zero-padded 3-digit gap-free sequence starting at 001.

All functions are side-effect-free imports except for write operations under
.aah/build/qa-results/.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aah.core.build import verification_profiles as _verification_profiles
from aah.core.common.attestation import write_attested
from aah.core.common.io_utils import read_json
from aah.core.common.sequenced_store import claim_sequenced_path, sequenced_paths
from aah.core.common.verified_artifacts import ArtifactState, load_attested_artifact


_canonical_config_hash = _verification_profiles.catalog_hash
profile_binding_for_feature = _verification_profiles.profile_binding_for_feature


# Canonical command prefix owned by verify.QA_REPORT_PREFIX.
QA_REPORT_PREFIX = ["aah", "run", "core.build.write_qa_report"]

def allocate_attempt(project_path: Path, feature_id: str) -> tuple[int, Path]:
    """Allocate next gap-free attempt number and return its path.

    Race-safe via O_CREAT|O_EXCL atomic claim. Scans existing attempts to
    find the next available number. If a collision occurs (race condition),
    re-scans and retries with the new max.

    Args:
        project_path: Project root containing .aah/
        feature_id: Feature identifier (e.g., "F001")

    Returns:
        (attempt_number, attempt_path) where attempt_number is 1-indexed and
        attempt_path is the absolute path to attempt-NNN.json

    Raises:
        RuntimeError: If unable to allocate after reasonable attempts
    """
    return claim_sequenced_path(
        project_path / ".aah" / "build" / "qa-results" / feature_id,
        "attempt-",
    )


def list_attempts(project_path: Path, feature_id: str) -> list[dict]:
    """List all verified attempts for a feature, sorted by attempt number.

    Only returns attempts that pass attestation verification with QA_REPORT_PREFIX.
    Malformed or unverifiable attempts are excluded (fail-closed).

    Args:
        project_path: Project root containing .aah/
        feature_id: Feature identifier (e.g., "F001")

    Returns:
        List of attempt payloads (dicts) with 'attempt' field added, sorted by
        attempt number ascending. Returns empty list if no verified attempts exist.
    """
    attempts_dir = project_path / ".aah" / "build" / "qa-results" / feature_id
    if not attempts_dir.exists():
        return []

    verified = []
    for attempt_num, candidate in sequenced_paths(attempts_dir, "attempt-"):
        artifact = load_attested_artifact(
            candidate, project_path, QA_REPORT_PREFIX, "json"
        )
        if artifact.state is not ArtifactState.VERIFIED:
            continue
        data = artifact.payload
        assert data is not None
        data["attempt"] = attempt_num
        verified.append(data)

    # Sort by attempt number (should already be sorted by filename, but ensure)
    verified.sort(key=lambda x: x["attempt"])
    return verified


def latest_attempt(project_path: Path, feature_id: str) -> dict | None:
    """Return the highest-numbered verified attempt, or None if no verified attempts exist.

    Args:
        project_path: Project root containing .aah/
        feature_id: Feature identifier (e.g., "F001")

    Returns:
        Latest verified attempt payload, or None if no verified attempts
    """
    attempts = list_attempts(project_path, feature_id)
    return attempts[-1] if attempts else None


def profile_snapshot_for_subject(
    project_path: Path, feature_id: str, subject_sha: str
) -> tuple[dict | None, str]:
    """Return the QA-time routing snapshot for an exact tested subject."""
    attempt = latest_attempt(project_path, feature_id)
    if attempt is None:
        return None, "qa_attempt_missing"
    recorded_sha = (attempt.get("subject") or {}).get("commit_sha")
    if recorded_sha != subject_sha:
        return None, "qa_attempt_stale_subject"
    snapshot = attempt.get("verification_profile")
    if not isinstance(snapshot, dict):
        # Legacy attempts predate routing snapshots. They stay approved and do
        # not inherit checks from mutable live .aah configuration.
        return {}, "legacy_profile_snapshot"
    no_signal = snapshot.get("no_signal")
    if isinstance(no_signal, str) and no_signal:
        return snapshot, f"qa_profile_{no_signal}"
    return snapshot, ""


def latest_human_decision(
    project_path: Path, feature_id: str, attempt: int | None
) -> dict | None:
    """Return the latest recorded user decision for one QA attempt."""
    decisions_dir = project_path / ".aah" / "build" / "qa-results" / feature_id
    if not decisions_dir.exists():
        return None

    target = next(
        (item for item in list_attempts(project_path, feature_id)
         if item.get("attempt") == attempt),
        None,
    )
    target_sha = ((target or {}).get("subject") or {}).get("commit_sha")
    recorded = []
    for decision_num, candidate in sequenced_paths(decisions_dir, "human-"):
        try:
            data = read_json(candidate)
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        decision_attempt = data.get("attempt")
        if decision_attempt != attempt:
            legacy_sha = (data.get("subject") or {}).get("commit_sha")
            if decision_attempt is not None or not target_sha or legacy_sha != target_sha:
                continue
        recorded.append((decision_num, data))
    return max(recorded, default=(0, None), key=lambda item: item[0])[1]


def derive_qa_state(project_path: Path, feature_id: str) -> dict:
    """Derive QA state from verified attempts — IDEMPOTENT pure function.

    Reads all verified attempts and computes:
    - attempts_total: count of all verified attempts
    - rework_count: count of attempts with verdict=="rework_required"
    - current_attempt: latest attempt number (or None)
    - current_verdict: verdict of latest attempt (or None)
    - current_subject_sha: commit_sha from latest attempt's subject (or None)
    - history: list of {attempt, verdict, subject_sha, timestamp, historical}

    An attempt is historical if:
    1. Its subject.commit_sha differs from current_subject_sha, OR
    2. It is not the latest attempt on the current SHA

    The latest attempt is always historical=False and defines current_verdict.

    Args:
        project_path: Project root containing .aah/
        feature_id: Feature identifier (e.g., "F001")

    Returns:
        QA state dict with the structure described above
    """
    attempts = list_attempts(project_path, feature_id)

    if not attempts:
        return {
            "feature_id": feature_id,
            "attempts_total": 0,
            "rework_count": 0,
            "current_attempt": None,
            "current_verdict": None,
            "current_subject_sha": None,
            "history": [],
        }

    latest = attempts[-1]
    current_sha = (latest.get("subject") or {}).get("commit_sha")
    current_verdict = latest.get("verdict")
    if current_verdict == "fail":
        current_verdict = "rework_required"
    current_attempt = latest["attempt"]

    rework_count = sum(
        1 for a in attempts if a.get("verdict") in ("fail", "rework_required")
    )

    # Build history with historical flag
    history = []
    for attempt in attempts:
        attempt_sha = (attempt.get("subject") or {}).get("commit_sha")
        is_latest = attempt["attempt"] == current_attempt

        # Historical if different SHA OR not the latest attempt
        is_historical = (attempt_sha != current_sha) or (not is_latest)

        history.append({
            "attempt": attempt["attempt"],
            "verdict": attempt.get("verdict"),
            "subject_sha": attempt_sha,
            "timestamp": attempt.get("timestamp"),
            "historical": is_historical,
        })

    return {
        "feature_id": feature_id,
        "attempts_total": len(attempts),
        "rework_count": rework_count,
        "current_attempt": current_attempt,
        "current_verdict": current_verdict,
        "current_subject_sha": current_sha,
        "history": history,
    }


def write_attempt_tests(
    project_path: Path,
    feature_id: str,
    attempt_no: int,
    tests_payload: dict[str, Any],
    *,
    command: list[str],
    duration_ms: int,
) -> Path:
    """Write attempt-NNN-tests.json with subject-bound reviewed test evidence.

    Copies the subject descriptor from the attempt's captured subject to ensure
    the test evidence is bound to the exact commit that was evaluated.

    Args:
        project_path: Project root containing .aah/
        feature_id: Feature identifier (e.g., "F001")
        attempt_no: Attempt number (1-indexed, must match an existing attempt-NNN.json)
        tests_payload: Test result payload to write (typically pytest output)
        command: Command that generated this evidence
        duration_ms: Execution duration in milliseconds

    Returns:
        Path to the written attempt-NNN-tests.json file

    Raises:
        RuntimeError: If the parent attempt-NNN.json doesn't exist or can't be read
    """
    attempts_dir = project_path / ".aah" / "build" / "qa-results" / feature_id
    attempt_path = attempts_dir / f"attempt-{attempt_no:03d}.json"

    # Read parent attempt to get subject descriptor
    if not attempt_path.exists():
        raise RuntimeError(
            f"Cannot write tests for attempt {attempt_no}: parent attempt-{attempt_no:03d}.json not found"
        )

    try:
        parent = json.loads(attempt_path.read_text(encoding="utf-8"))
        subject = parent.get("subject")
    except Exception as e:
        raise RuntimeError(
            f"Cannot read parent attempt-{attempt_no:03d}.json: {e}"
        ) from e

    # Build tests evidence with subject binding
    tests_evidence = {
        "feature_id": feature_id,
        "attempt": attempt_no,
        "subject": subject,  # Exact subject copy from parent attempt
        "tests": tests_payload,
    }

    tests_path = attempts_dir / f"attempt-{attempt_no:03d}-tests.json"
    write_attested(
        tests_evidence,
        tests_path,
        project_path=project_path,
        command=command,
        exit_code=0,
        stdout="",
        stderr="",
        duration_ms=duration_ms,
        artifact_name=f"{feature_id} QA test evidence (attempt {attempt_no})",
    )

    return tests_path
