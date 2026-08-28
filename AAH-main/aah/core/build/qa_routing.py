#!/usr/bin/env python3
"""
Per-feature QA routing: where does one feature stand against its subject?

The orchestrator asks two questions of QA and this module answers both:
``_feature_qa_state_action`` decides what should happen to a feature next, and
``_feature_qa_final_approved`` decides whether it may pass the merge gate. Both
are strictly subject-bound — an attempt is evidence only for the SHA it ran
against — and both fail closed: a missing or unreadable verification profile is
``no_signal``, never an inferred ``standard``.

Human decisions here are keyed on the QA **attempt**, matching what
``validate_checkpoint._record_feature_qa_decision`` writes. A reader keyed on
anything else finds no approval, ever, and every wave hangs at
``human_review_required`` with no error to explain it.

A QA pass does not add a second per-feature human gate; the configured wave
review owns the single interactive user approval.

This module imports from ``verify`` / ``verification_profiles`` / ``qa_evidence``
/ ``common`` and imports nothing from ``orchestrator``. That direction is the
point: ``orchestrator.py`` imports these names back for its gate cascade.
"""

from pathlib import Path

from aah.core.build.qa_evidence import (
    latest_human_decision,
    profile_snapshot_for_subject,
)
from aah.core.build.verification_profiles import (
    effective_profile_level,
    profile_binding_for_feature,
)
from aah.core.build.verify import (
    FEATURE_TEST_PREFIX,
    read_attested,
    resolve_feature_subject,
)
from aah.core.build.verification_evidence import (
    feature_qa_approval_validation,
    feature_qa_state_validation,
)
from aah.core.common.feature_utils import load_wave_feature_ids
from aah.core.common.progress import load_progress

# A feature whose QA keeps coming back rework_required must escalate to a human
# after this many attempts rather than looping QA forever.
MAX_QA_REWORK_ATTEMPTS = 3


def feature_qa_review_commands(feature_id: str) -> dict[str, str]:
    """Return the two operator commands for a feature-level QA decision."""
    return {
        "approve_command": (
            f"aah run core.build.validate_checkpoint feature-qa-approve "
            f"--feature-id {feature_id}"
        ),
        "request_rework_command": (
            f"aah run core.build.validate_checkpoint feature-qa-request-rework "
            f"--feature-id {feature_id} --rationale \"<text>\""
        ),
    }


def _feature_profile_level(
    project_path: Path, feature_id: str, subject_sha: str
) -> dict:
    """Compute the effective verification-profile routing for a feature.

    Returns a dict::

        {
          "level": "standard" | "deep" | None,
          "no_signal": <reason str> | None,
          "binding": <profile binding dict> | None,
        }

    A missing or invalid profile is ``no_signal``; ``standard`` is never
    inferred. A verified lowering override may change deep to standard, but a
    lowering is never inferred from QA or human artifacts.
    """
    binding = profile_binding_for_feature(
        project_path, feature_id, subject_sha=subject_sha
    )
    if binding.get("no_signal") is not None:
        return {
            "level": None,
            "no_signal": binding["no_signal"],
            "binding": binding,
        }
    level = effective_profile_level(binding)
    return {"level": level, "no_signal": None, "binding": binding}


def _human_review_action(
    feature_id: str,
    subject: dict,
    state: dict,
    *,
    trigger: str,
    reason: str,
) -> dict:
    """Build the ``human_review_required`` action for a per-feature QA escalation.

    Single-sources the two ``feature-qa-*`` operator commands. They carry ONLY
    ``--feature-id`` (plus ``--rationale`` on rework) because that is exactly
    what ``validate_checkpoint``'s parsers accept — an extra flag here makes the
    emitted command exit non-zero under argparse, unattended and unnoticed.

    ``wave`` is left ``None`` for ``compute_next_action`` to fill in.
    """
    current_attempt = state.get("current_attempt")
    return {
        "action": "human_review_required",
        "wave": None,  # Filled in by caller
        "feature_id": feature_id,
        "subject_branch": subject.get("subject_branch"),
        "subject_path": subject.get("subject_path"),
        "trigger": trigger,
        "attempts_total": state.get("attempts_total", 0),
        "rework_count": state.get("rework_count", 0),
        "qa_state": state,
        "authoritative_attempt": (
            f"qa-results/{feature_id}/attempt-{current_attempt:03d}.json"
        ),
        **feature_qa_review_commands(feature_id),
        "reason": reason,
    }


def _feature_qa_state_action(
    project_path: Path,
    aah_path: Path,
    feature_id: str,
    subject: dict,
) -> str | dict:
    """Derive QA state action for a feature given its resolved subject.

    Imports derive_qa_state from qa_evidence to compute current state, then
    branches on attempt status to return either a sentinel string or an action
    dict.

    Args:
        project_path: Project root
        aah_path: Path to .aah/
        feature_id: Feature identifier (e.g., "F001")
        subject: Resolved subject dict with subject_path, subject_branch, subject_sha

    Returns:
        One of:
        - "run_qa": sentinel indicating fresh attempt needed (no attempt for current SHA)
        - "approved": feature has passing QA or human approval
        - "rework": feature needs rework (below cap or human requested)
        - dict: human_review_required action payload
    """
    subject_sha = subject["subject_sha"]
    state, state_problem = feature_qa_state_validation(
        project_path, feature_id, subject_sha,
    )

    current_attempt = state.get("current_attempt")
    current_verdict = state.get("current_verdict")
    rework_count = state.get("rework_count", 0)

    # Branch 1: No attempt for current SHA. Resolve routing once, before QA;
    # later .aah profile edits never invalidate the attempt that snapshots it.
    if state_problem is not None:
        profile = _feature_profile_level(project_path, feature_id, subject_sha)
        if profile["no_signal"] is not None:
            return {
                "action": "error",
                "feature_id": feature_id,
                "subject_branch": subject.get("subject_branch"),
                "subject_path": subject.get("subject_path"),
                "trigger": "profile_configuration_error",
                "reason": (
                    "Verification profile unavailable before QA "
                    f"({profile['no_signal']}). Regenerate checkpoint configuration."
                ),
            }
        return "run_qa"

    _snapshot, snapshot_problem = profile_snapshot_for_subject(
        project_path, feature_id, subject_sha
    )
    if snapshot_problem.startswith("qa_profile_"):
        return {
            "action": "error",
            "feature_id": feature_id,
            "subject_branch": subject.get("subject_branch"),
            "subject_path": subject.get("subject_path"),
            "trigger": "profile_configuration_error",
            "reason": (
                "QA attempt captured an unusable verification profile "
                f"({snapshot_problem}). Correct the configuration before a new QA run."
            ),
        }

    # Branch 2: Latest attempt is for current SHA — verdict-specific routing
    if current_verdict == "pass":
        # A QA pass does not add a second per-feature human gate. Configured
        # wave review owns the single interactive user approval.
        return "approved"

    if current_verdict == "human_review_required":
        # Branch (d): check for human decision
        pass  # Fall through to human gate below

    if current_verdict == "rework_required":
        if rework_count >= MAX_QA_REWORK_ATTEMPTS:
            # CAP: never re-QA, never auto-pass → escalate to human
            pass  # Fall through to human gate below
        else:
            # Below cap → dispatch implementer for rework
            return "rework"

    # Branch (d): human gate — check for human decision
    human = latest_human_decision(project_path, feature_id, current_attempt)
    if human is not None:
        decision = human.get("decision")
        if decision == "approve":
            # NOTE: a recorded user approval must never waive a failed objective
            # contract check. No objective per-feature contract gate exists on
            # this tree; whoever adds one must run it here, ahead of this return.
            return "approved"
        elif decision == "request_rework":
            return "rework"

    # No human decision yet → return human_review_required action
    # Determine trigger
    if current_verdict == "rework_required" and rework_count >= MAX_QA_REWORK_ATTEMPTS:
        trigger = "rework_cap"
        reason = f"Feature {feature_id} has reached the rework cap ({MAX_QA_REWORK_ATTEMPTS} attempts). Human review required."
    else:
        trigger = "qa_verdict"
        reason = f"QA verdict is '{current_verdict}'. Human review required before proceeding."

    return _human_review_action(
        feature_id, subject, state, trigger=trigger, reason=reason
    )


def _feature_qa_final_approved(
    project_path: Path,
    aah_path: Path,
    feature_id: str,
) -> bool:
    """True iff feature has final QA approval for its current subject SHA.

    Used by wave_completed derivation at the merge gate. The QA attempt remains
    subject-bound; an exceptional user choice is a simple record associated
    with that attempt rather than a separately verified artifact.

    Strictly per-current-wave — does NOT touch global completed set.

    Args:
        project_path: Project root
        aah_path: Path to .aah/
        feature_id: Feature identifier (e.g., "F001")

    Returns:
        True iff feature has final approval for merge
    """
    # Determine wave context to resolve the authoritative subject.
    progress = load_progress(aah_path / "claude-progress.json" if (aah_path / "claude-progress.json").exists() else None)
    current_wave = progress.get("current_wave") or 0
    wave_feature_ids = load_wave_feature_ids(aah_path, current_wave)
    if feature_id not in wave_feature_ids:
        return False

    total_features_in_wave = len(wave_feature_ids)

    # Resolve subject
    subject_result = resolve_feature_subject(
        project_path, feature_id, total_features_in_wave
    )
    if not subject_result.get("ok"):
        return False

    subject_sha = subject_result["subject_sha"]

    _attempt, problem = feature_qa_approval_validation(
        project_path, feature_id, subject_sha
    )
    return problem is None


def _features_needing_qa(project_path: Path, aah_path: Path, wave_remaining: list[str]) -> list[str]:
    """Find features that need independent aah-qa-evaluator review.

    Mandatory-QA contract: QA is NOT optional. Every feature with attested
    passing per-feature tests and no verified QA approval is dispatched to
    the independent aah-qa-evaluator agent. The agent performs an independent
    semantic review over the verified subject-bound feature-test evidence.

    Per-feature test result files are read via attested verification —
    a forged passing result will not advance the feature to QA dispatch.
    Likewise, an existing QA report is honored only when its attestation
    verifies.

    QA approval is always SHA-aware: a feature is treated as approved only when
    its approval is for the current subject SHA. This keeps enqueue behavior
    consistent with the merge gate (_feature_qa_final_approved).
    """
    needing_qa = []
    test_results_dir = aah_path / "build" / "test-results"
    if not test_results_dir.is_dir():
        return []

    for fid in wave_remaining:
        # Check if feature has test results (implementation done) with passing tests
        result_file = test_results_dir / f"{fid}.json"
        if not result_file.exists():
            continue
        result = read_attested(result_file, aah_path.parent, FEATURE_TEST_PREFIX)
        if result is None:
            # Forged or legacy — orchestrator will re-trigger feature
            # tests via the dispatch path; do not enqueue for QA yet.
            continue
        if not result.get("passed"):
            continue

        if _feature_qa_final_approved(project_path, aah_path, fid):
            continue

        # Traceability passes and no verified QA report exists → QA is
        # mandatory. Dispatch to independent aah-qa-evaluator.
        needing_qa.append(fid)
    return needing_qa
