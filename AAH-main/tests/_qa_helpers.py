"""Shared NO-MOCKS helper for attested QA-attempt evidence tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.attestation import write_attested


QA_REPORT_PREFIX = ["aah", "run", "core.build.write_qa_report"]


def seed_qa_attempt(
    project: Path,
    feature_id: str,
    attempt_no: int,
    verdict: str,
    subject_sha: str,
) -> Path:
    """Write a REAL attested attempt-NNN.json (verifies under QA_REPORT_PREFIX)."""
    d = project / ".aah" / "build" / "qa-results" / feature_id
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"attempt-{attempt_no:03d}.json"
    payload = {
        "schema_version": 1,
        "feature_id": feature_id,
        "attempt": attempt_no,
        "verdict": verdict,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "subject": {"commit_sha": subject_sha, "branch": "feature/x", "clean": True},
        "summary": {"criteria_total": 1, "criteria_passed": 0 if verdict != "pass" else 1},
    }
    from aah.core.build.verification_profiles import profile_binding_for_feature

    payload["verification_profile"] = profile_binding_for_feature(
        project, feature_id, subject_sha=subject_sha
    )
    write_attested(
        payload,
        path,
        project_path=project,
        command=QA_REPORT_PREFIX + ["--feature-id", feature_id, "--verdict", verdict],
        exit_code=0, stdout="", stderr="", duration_ms=0,
        artifact_name=f"{feature_id} QA attempt {attempt_no}",
    )
    return path
