#!/usr/bin/env python3
"""Shared subject/input-envelope validation for feature evidence.

This module is the freshness-and-binding validator behind
``verification_evidence.feature_evidence_freshness_problem``: given an attested
feature-evidence payload, it answers whether that payload is bound to the exact
clean subject the caller expects and whether the contract and test inputs it was
produced against are still current.

The build-time spec-to-implementation validation that used to live here — the
AC -> TC -> collected-test join, its ``<fid>-spec-validation.json`` artifact, and
the ``validate`` CLI — is gone. That join now runs at its two natural endpoints:
``validators.validate_feature_contract`` proves every acceptance criterion has a
planned test case at planning time, and ``run_feature_tests`` proves every
planned test case was actually collected during the scoped test run.

There is no CLI here. The single exported helper is called in process.
"""

from pathlib import Path

from aah.core.build.evidence import (
    EvidenceError,
    capture_subject,
    evidence_is_fresh,
    evidence_matches_binding,
    hash_feature_contract,
    hash_test_inputs,
)


def _feature_subject_evidence_problem(
    evidence: dict,
    *,
    project_path: Path,
    subject_path: Path,
    feature_id: str,
    expected_branch: str | None,
    expected_sha: str | None,
) -> str | None:
    """Validate the shared subject/input envelope after attestation."""
    if not evidence_matches_binding(
        evidence,
        feature_id=feature_id,
        expected_branch=expected_branch or "",
        expected_sha=expected_sha or "",
    ):
        return "Evidence does not match feature/branch/SHA/clean binding"

    inputs = evidence.get("inputs")
    if not isinstance(inputs, dict):
        return "Input evidence is missing or malformed"
    test_paths = inputs.get("test_paths")
    if not isinstance(test_paths, list) or not all(isinstance(path, str) for path in test_paths):
        return "Input paths are missing or malformed"

    try:
        current_subject = capture_subject(subject_path, project_path)
        current_binding = {
            "schema_version": 2,
            "feature_id": feature_id,
            "subject": current_subject,
        }
        if not evidence_matches_binding(
            current_binding,
            feature_id=feature_id,
            expected_branch=expected_branch or "",
            expected_sha=expected_sha or "",
        ):
            return "Current subject does not match expected branch/SHA or is dirty"

        from aah.core.common.feature_utils import find_feature_file

        feature_path = find_feature_file(
            subject_path / ".aah" / "plan" / "features", feature_id
        )
        if not feature_path:
            return f"Feature contract not found for {feature_id}"
        contract_hash = hash_feature_contract(feature_path)
        test_files = [(subject_path / rel_path) for rel_path in test_paths]
        test_input_hash = hash_test_inputs(test_files, subject_path)
    except (EvidenceError, OSError) as exc:
        return str(exc)

    stored_subject = evidence.get("subject", {})
    stored = {
        "commit_sha": stored_subject.get("commit_sha"),
        "contract_hash": inputs.get("contract_hash"),
        "test_input_hash": inputs.get("test_input_hash"),
    }
    if not evidence_is_fresh(stored, current_subject, contract_hash, test_input_hash):
        return "Evidence is stale for the current subject or inputs"
    return None
