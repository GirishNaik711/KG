#!/usr/bin/env python3
"""Final read-only evidence verifier for a wave.

Answers ONE question: *is the complete required evidence set for wave N present,
attested, fresh, and passing on the current integration branch?* It is the last
gate before the orchestrator returns ``merge``, and the first gate inside
``promote_to_develop`` before the irreversible checkout.

READ-ONLY: it does not run tests, boot the app, run Docker, call agents, repair
evidence, switch branches, or merge. The producers write the evidence; this
module only reads and judges it. It writes nothing, so it signs nothing.

Called in process so a structured exception can cross the boundary — a
subprocess could only return an exit code, losing the failure list.

Three identity questions, three mechanisms. Only the third lives here:

  * Was *this code* evaluated? → ``code_subject_identity()``
  * Against the current *per-feature* criteria? → ``evidence_is_fresh()``
  * Against the current *runtime* criteria? → ``runtime_criteria_identity()``

Manual CLI (operators and focused tests only; orchestration calls the function):

    aah run core.build.verify --project-path PROJECT --wave N
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from aah.core.build.verification_contracts import (
    ACTION_BLOCKED,
    ACTION_FIX_REGRESSION,
    ACTION_FIX_RUNTIME,
    ACTION_FIX_STANDARDS,
    ACTION_GENERATE_ARTIFACTS,
    ACTION_HUMAN_REVIEW,
    ACTION_NO_SIGNAL,
    ACTION_REVERIFY,
    ACTION_RUN_FEATURE_TESTS,
    ACTION_RUN_QA,
    ACTION_RUN_REGRESSION,
    ACTION_RUN_STANDARDS,
    ACTION_RUN_SYSTEM_CHECKPOINT,
    ACTION_UPDATE_EXPERTISE,
    ACTION_USER_REVIEW,
    ACTION_WAVE_SUMMARY,
    VerificationFailure,
    VerificationReport,
    VerificationSystemError,
    WaveVerificationFailed,
    verification_failure_sort_key,
)
from aah.core.build.verification_evidence import (
    FEATURE_TEST_PREFIX,
    NO_VERDICT_STATUSES,
    QA_REPORT_PREFIX,
    QUALITY_PREFIX,
    REGRESSION_PREFIX,
    RUNTIME_RESULTS_PREFIX,
    RUNTIME_RESULTS_PREFIXES,
    feature_evidence_freshness_problem,
    feature_has_passed_attested_tests,
    feature_qa_approval_validation,
    read_attested,
    read_fresh_feature_evidence,
    read_regression_evidence,
    regression_evidence_problem,
    resolve_feature_subject,
    standards_evidence_passed,
)
from aah.core.build.verification_identity import (
    canonical_payload_sha256,
    file_sha256,
    runtime_criteria_identity,
    runtime_profile_evidence_validation,
    subject_identity,
    verification_report_is_current,
)
from aah.core.build.verification_profiles import load_profile_catalog
from aah.core.common.feature_utils import load_wave_feature_ids
from aah.core.common.io_utils import read_json, read_yaml
from aah.core.common.git_utils import GitError, current_branch
from aah.core.common.verified_artifacts import ArtifactState, load_attested_artifact


# ---------------------------------------------------------------------------
# Per-scope evidence validation
# ---------------------------------------------------------------------------


class _Collector:
    """Accumulates failures and consulted-artifact hashes for one scan.

    Never stops at the first failure — the whole point of the report is that an
    operator sees every defect at once.
    """

    def __init__(self, project_path: Path, wave: int):
        self.project_path = project_path
        self.wave = wave
        self.failures: list[VerificationFailure] = []
        self.artifacts: dict[str, str] = {}

    def fail(
        self, *, code: str, artifact: Path | str, message: str, action: str,
        feature_id: str | None = None, details: dict | None = None,
    ) -> None:
        self.failures.append(VerificationFailure(
            code=code, artifact=self._rel(artifact), message=message,
            action=action, wave=self.wave, feature_id=feature_id,
            details=details or {},
        ))

    def _rel(self, artifact: Path | str) -> str:
        if isinstance(artifact, str):
            return artifact
        try:
            return str(artifact.relative_to(self.project_path))
        except ValueError:
            return str(artifact)

    def record_payload(self, path: Path, payload: Any) -> None:
        self.artifacts[self._rel(path)] = canonical_payload_sha256(payload)

    def record_file(self, path: Path) -> None:
        digest = file_sha256(path)
        if digest is not None:
            self.artifacts[self._rel(path)] = digest

    def read(
        self,
        path: Path,
        prefixes: list[str] | list[list[str]],
        *,
        code_missing: str,
        action_missing: str,
        message_missing: str,
        feature_id: str | None = None,
        reverify_command: str | None = None,
        details: dict | None = None,
    ) -> dict | None:
        """Read attested evidence, recording the right failure when it fails.

        A rotated-secret defect becomes `reverify_evidence` (cheap re-sign)
        rather than a full producer re-run — but only when a command is supplied.
        """
        artifact = load_attested_artifact(
            path, self.project_path, prefixes, "json"
        )
        if artifact.state is ArtifactState.VERIFIED:
            assert artifact.payload is not None
            self.record_payload(path, artifact.payload)
            return artifact.payload
        if artifact.state is ArtifactState.STALE_SECRET and reverify_command:
            failure_details = dict(details or {})
            failure_details.update(
                command=reverify_command,
                reason=artifact.reason,
            )
            self.fail(
                code="evidence_stale_secret", artifact=path,
                message=(
                    f"{path.name} was signed with a rotated session secret "
                    f"({artifact.reason}). Re-sign it with the original writer."
                ),
                action=ACTION_REVERIFY, feature_id=feature_id,
                details=failure_details,
            )
        else:
            failure_details = dict(details or {})
            if reverify_command:
                failure_details["command"] = reverify_command
            self.fail(
                code=code_missing, artifact=path, message=message_missing,
                action=action_missing, feature_id=feature_id,
                details=failure_details,
            )
        return None


def _check_branch(col: _Collector) -> str | None:
    """Require the current branch to be integration/wave-N. Returns it, or None."""
    expected = f"integration/wave-{col.wave}"
    try:
        actual = current_branch(cwd=col.project_path)
    except GitError as exc:
        col.fail(
            code="branch_unreadable", artifact="<git>",
            message=f"Could not determine the current branch: {exc}",
            action=ACTION_BLOCKED,
        )
        return None
    if actual != expected:
        col.fail(
            code="wrong_branch", artifact="<git>",
            message=(
                f"Verification requires {expected} but the current branch is "
                f"{actual}. Evidence written on one branch is invisible from "
                "another — check out the integration branch and re-run."
            ),
            action=ACTION_BLOCKED,
            details={"expected_branch": expected, "actual_branch": actual},
        )
        return None
    return expected


def _check_features(col: _Collector, feature_ids: list[str]) -> None:
    """Per-feature evidence — DISABLED (§3). Verifies nothing per feature.

    Both per-feature checks are no longer made:

      * the attested feature-test evidence check
        (``_check_declared_feature_evidence`` + ``FEATURE_TEST_EVIDENCE``), and
      * the QA attempt/verdict check (``_check_feature_qa``).

    DISABLED MEANS NOT CALLED, NOT DELETED. Both functions, the
    ``FEATURE_TEST_EVIDENCE`` spec, ``feature_qa_approval_validation``, and every
    failure code they emit stay defined and unmodified — flipping
    ``orchestrator.PER_FEATURE_QA_ENABLED`` back to True and restoring the loop
    body re-arms them.

    This is REQUIRED, not cosmetic: the producers of both artifacts are no longer
    invoked, so leaving either call would make verify fail closed on
    ``qa_attempt_missing`` / ``feature_test_missing`` and no wave could ever
    promote. With nothing left in the loop body the function returns without
    work; it is kept (rather than removed from ``verify_wave_evidence``) so the
    scan's shape — and the diff to re-arm it — stays obvious.

    Standards was never here. It is project-scoped and verified once, in the last
    wave, by ``_check_project_standards``. Regression and runtime are likewise
    wave-scoped and cadence-gated. Those wave-level gates, not this function, are
    what now carry the whole automated signal.
    """
    from aah.core.build.orchestrator import PER_FEATURE_QA_ENABLED

    if not PER_FEATURE_QA_ENABLED:
        return

    aah = col.project_path / ".aah"
    for fid in sorted(feature_ids):
        resolved = resolve_feature_subject(col.project_path, fid, len(feature_ids))
        if not resolved.get("ok"):
            col.fail(
                code="feature_subject_unavailable",
                artifact=f"feature/{fid}",
                message=f"Could not resolve a clean subject for {fid}: {resolved.get('reason')}",
                action=ACTION_NO_SIGNAL,
                feature_id=fid,
                details={"signal_reason": "feature_subject_unavailable"},
            )
            continue
        _check_declared_feature_evidence(col, fid, resolved, FEATURE_TEST_EVIDENCE)
        _check_feature_qa(col, aah, fid, resolved)


@dataclass(frozen=True)
class _FeatureEvidenceSpec:
    """Declarative contract for one feature-scoped evidence artifact."""

    artifact: str
    prefix: list[str]
    missing_code: str
    missing_action: str
    missing_message: str
    reverify_command: str
    rejects: Callable[[Mapping[str, Any]], bool]
    failure_code: str
    failure_action: str
    failure_message: str
    stale_code: str | None = None
    stale_action: str | None = None
    stale_message: str | None = None
    carries_signal_reason: bool = False
    freshness_v2_only: bool = False


FEATURE_TEST_EVIDENCE = _FeatureEvidenceSpec(
    artifact=".aah/build/test-results/{fid}.json",
    prefix=FEATURE_TEST_PREFIX,
    missing_code="feature_test_missing",
    missing_action=ACTION_GENERATE_ARTIFACTS,
    missing_message="No verified feature-test evidence for {fid}. Run the feature tests.",
    reverify_command=(
        "aah run core.build.run_feature_tests --feature-id {fid} "
        "--project-path {project_path} --subject-path {subject_path} "
        "--subject-branch {subject_branch}"
    ),
    rejects=lambda data: not data.get("passed"),
    failure_code="feature_test_failed",
    failure_action=ACTION_BLOCKED,
    failure_message=(
        "Feature-test evidence for {fid} reports passed=false at the final gate. "
        "Re-running the tests cannot fix this — the failure needs a human or a "
        "rework decision."
    ),
    stale_code="feature_test_stale",
    stale_action=ACTION_RUN_FEATURE_TESTS,
    stale_message="Feature-test evidence for {fid} is stale: {reason}.",
)


def _check_project_standards(col: _Collector, subject: str) -> None:
    """Whole-codebase standards evidence — LAST WAVE ONLY.

    Gated on "is the final wave" because the gate runs once per project. On any
    earlier wave this is a no-op, NOT a missing-evidence failure: demanding
    evidence that is only produced in the last wave would block every wave
    before it.

    A completed run with no verdict routes to ``no_signal``. A failing verdict
    routes to ``fix_standards`` (a repair), not ``blocked``. Lint is
    mechanically repairable; a genuine security finding escalates through the
    decision loop, which is intended.
    """
    from aah.core.build.orchestrator import is_last_wave
    from aah.core.build.quality_checks import project_standards_artifact

    aah = col.project_path / ".aah"
    if not is_last_wave(aah, col.wave):
        return

    path = project_standards_artifact(aah, col.wave)
    command = (
        "aah run core.build.quality_checks run-project "
        f"--project-path . --wave {col.wave}"
    )
    data = col.read(
        path,
        QUALITY_PREFIX,
        code_missing="project_standards_missing",
        action_missing=ACTION_RUN_STANDARDS,
        message_missing=(
            "No verified whole-codebase standards evidence for the final wave. "
            "Run the standards check before promotion."
        ),
        reverify_command=command,
    )
    if data is None:
        return

    recorded = data.get("subject") if isinstance(data.get("subject"), dict) else {}
    if recorded.get("commit_sha") != subject:
        col.fail(
            code="project_standards_stale",
            artifact=path,
            message=(
                "Whole-codebase standards evidence is bound to a subject that "
                f"is no longer current (recorded {recorded.get('commit_sha')}, "
                f"current {subject}). Re-run it."
            ),
            action=ACTION_RUN_STANDARDS,
            details={"command": command},
        )
        return

    if str(data.get("status")) == "no_signal":
        col.fail(
            code="project_standards_no_signal",
            artifact=path,
            message=(
                "Whole-codebase standards completed but could not render a "
                "verdict. Resolve the unavailable tool or environment before "
                "retrying."
            ),
            action=ACTION_NO_SIGNAL,
            details={"signal_reason": "project_standards_no_signal"},
        )
        return

    if not standards_evidence_passed(data):
        findings = data.get("findings") if isinstance(data.get("findings"), dict) else {}
        col.fail(
            code="project_standards_failed",
            artifact=path,
            message=(
                "Whole-codebase standards evidence reports a blocking verdict. "
                "Route it through aah-fix to repair the integration branch."
            ),
            action=ACTION_FIX_STANDARDS,
            details={
                "command": command,
                "failures": {
                    "linting": findings.get("linting", ""),
                    "static_analysis": findings.get("static_analysis", ""),
                },
            },
        )


def _check_declared_feature_evidence(
    col: _Collector,
    fid: str,
    subject: Mapping[str, Any],
    spec: _FeatureEvidenceSpec,
) -> None:
    """Apply a feature evidence contract without executing its producer."""
    path = col.project_path / spec.artifact.format(fid=fid)
    reverify_command = spec.reverify_command.format(
        fid=shlex.quote(fid),
        project_path=shlex.quote(str(col.project_path)),
        subject_path=shlex.quote(str(subject["subject_path"])),
        subject_branch=shlex.quote(str(subject["subject_branch"])),
        subject_sha=shlex.quote(str(subject["subject_sha"])),
    )
    data = col.read(
        path,
        spec.prefix,
        code_missing=spec.missing_code,
        action_missing=spec.missing_action,
        message_missing=spec.missing_message.format(fid=fid),
        feature_id=fid,
        reverify_command=reverify_command,
    )
    if data is None:
        return

    if spec.stale_code and (
        not spec.freshness_v2_only or data.get("schema_version") == 2
    ):
        freshness_reason = feature_evidence_freshness_problem(
            data,
            project_path=col.project_path,
            subject_path=Path(str(subject["subject_path"])),
            feature_id=fid,
            expected_branch=str(subject["subject_branch"]),
            expected_sha=str(subject["subject_sha"]),
        )
        if freshness_reason:
            col.fail(
                code=spec.stale_code,
                artifact=path,
                message=(spec.stale_message or "{reason}").format(
                    fid=fid, reason=freshness_reason,
                ),
                action=spec.stale_action or ACTION_BLOCKED,
                feature_id=fid,
                details={"command": reverify_command},
            )
            return

    if spec.rejects(data):
        reason = str(data.get("reason") or "no reason recorded")
        col.fail(
            code=spec.failure_code,
            artifact=path,
            message=spec.failure_message.format(fid=fid, reason=reason),
            action=spec.failure_action,
            feature_id=fid,
            details={"signal_reason": data.get("reason") or "no_signal"}
            if spec.carries_signal_reason else None,
        )


def _check_feature_qa(
    col: _Collector, aah: Path, fid: str, subject: Mapping[str, Any]
) -> None:
    """Authoritative QA attempt + its matching QA-test evidence.

    The append-only attempt files are the only QA authority.
    """
    project_path = col.project_path
    expected_subject_sha = str(subject["subject_sha"])
    qa_details = {
        "subject": {
            key: subject[key]
            for key in ("subject_path", "subject_branch", "subject_sha")
        }
    }
    attempt, approval_problem = feature_qa_approval_validation(
        project_path, fid, expected_subject_sha,
    )
    if attempt is None:
        col.fail(
            code="qa_attempt_missing",
            artifact=f".aah/build/qa-results/{fid}/",
            message=f"No verified QA attempt exists for {fid}. Run QA.",
            action=ACTION_RUN_QA,
            feature_id=fid,
            details=qa_details,
        )
        return
    attempt_no = int(attempt["attempt"])

    attempt_dir = aah / "build" / "qa-results" / fid
    attempt_path = attempt_dir / f"attempt-{attempt_no:03d}.json"
    tests_path = attempt_dir / f"attempt-{attempt_no:03d}-tests.json"
    col.record_file(attempt_path)

    tests_data = col.read(
        tests_path,
        QA_REPORT_PREFIX,
        code_missing="qa_attempt_tests_missing",
        action_missing=ACTION_RUN_QA,
        message_missing=(
            f"QA attempt {attempt_no} for {fid} has no matching verified "
            f"{tests_path.name}. The attempt's rerun evidence is required."
        ),
        feature_id=fid,
        details=qa_details,
    )

    recorded_sha = (attempt.get("subject") or {}).get("commit_sha")
    if approval_problem == "qa_attempt_stale_subject":
        col.fail(
            code="qa_attempt_stale_subject",
            artifact=attempt_path,
            message=(
                f"QA attempt {attempt_no} for {fid} is bound to {recorded_sha}, "
                f"not the current feature subject {expected_subject_sha}."
            ),
            action=ACTION_RUN_QA,
            feature_id=fid,
            details=qa_details,
        )
        return

    if approval_problem and approval_problem.startswith("qa_profile_"):
        col.fail(
            code=approval_problem,
            artifact=attempt_path,
            message=(
                f"QA attempt {attempt_no} for {fid} captured no usable "
                "verification-profile routing snapshot."
            ),
            action=ACTION_BLOCKED,
            feature_id=fid,
            details={"attempt": attempt_no, "reason": approval_problem},
        )
        return

    if approval_problem:
        verdict = str(attempt.get("verdict") or "").lower()
        col.fail(
            code=("qa_rework_required" if verdict in (
                "rework_required", "fail", "human_review_required"
            ) else "qa_verdict_not_pass"),
            artifact=attempt_path,
            message=(
                f"QA attempt {attempt_no} for {fid} has verdict {verdict!r}. "
                "Human review is required before promotion."
            ),
            action=ACTION_HUMAN_REVIEW,
            feature_id=fid,
            details={
                "attempt": attempt_no,
                "verdict": verdict,
            },
        )
        return

    # The rerun evidence must be bound to the same subject as its attempt.
    if tests_data is not None:
        tests_sha = (tests_data.get("subject") or {}).get("commit_sha")
        if tests_sha != recorded_sha:
            col.fail(
                code="qa_tests_subject_mismatch",
                artifact=tests_path,
                message=(
                    f"QA test evidence for {fid} is bound to a different subject "
                    f"than its attempt ({tests_sha} vs {recorded_sha})."
                ),
                action=ACTION_RUN_QA,
                feature_id=fid,
                details=qa_details,
            )


def _check_profile_catalog(col: _Collector) -> None:
    """Block the wave when the verification profile catalog cannot be loaded.

    This is the ONLY place ``verify_wave_evidence`` fails closed on the catalog
    itself, as opposed to on one feature's entry within it. Standard/deep routing
    reads the same catalog per feature and degrades to ``no_signal``; without this
    check a wave whose ``checkpoint-config.yaml`` is missing or malformed would
    reach the merge gate with no profile signal at all and nothing objecting.
    """
    profiles, catalog_reason = load_profile_catalog(col.project_path)
    if profiles is None:
        col.fail(
            code="verification_profiles_missing",
            artifact=".aah/plan/checkpoint-config.yaml",
            message=f"The verification profile catalog could not be loaded ({catalog_reason}).",
            action=ACTION_BLOCKED,
        )


def _check_regression_evidence(col: _Collector, subject: str, branch: str) -> None:
    """Regression evidence — LAST WAVE ONLY. `status` is read BEFORE `passed`.

    Gated on "is the final wave" exactly like ``_check_project_standards``,
    because regression now runs once per project over the cumulative suite. On
    any earlier wave this is a no-op, NOT a missing-evidence failure: demanding
    evidence that is only produced in the last wave would mean verify never
    passes and no wave could ever promote.

    ``passed: false`` alone cannot distinguish "the suite failed" from "the
    suite never ran", and those route to different actions — a fix dispatched
    for a suite that never ran repairs a non-existent failure.
    """
    from aah.core.build.orchestrator import is_last_wave

    if not is_last_wave(col.project_path / ".aah", col.wave):
        return

    path = col.project_path / ".aah" / "build" / "test-results" / "regression-latest.json"
    command = f"aah run core.build.run_regression_suite --wave {col.wave}"
    data = col.read(
        path,
        REGRESSION_PREFIX,
        code_missing="regression_missing",
        action_missing=ACTION_RUN_REGRESSION,
        message_missing=(
            "No verified regression evidence for this wave. Run the regression suite."
        ),
        reverify_command=command,
    )
    if data is None:
        return

    status = data.get("status", "pass" if data.get("passed") else "fail")

    if status in NO_VERDICT_STATUSES:
        col.fail(
            code="regression_no_signal",
            artifact=path,
            message=(
                "Regression did not render a verdict "
                f"({data.get('signal_reason') or status}): "
                f"{data.get('message', 'no detail recorded')}"
            ),
            action=ACTION_NO_SIGNAL,
            details={
                "status": status,
                "signal_reason": data.get("signal_reason") or status,
            },
        )
        return

    # Regression is matched on SUBJECT ONLY — it stores no criteria identity.
    problem = regression_evidence_problem(data, branch, subject)
    if problem:
        code, message = problem
        final_code = {
            "subject_block_missing": "regression_subject_missing",
            "subject_branch_mismatch": "regression_wrong_branch",
            "subject_sha_stale": "regression_stale_subject",
        }.get(code, code)
        col.fail(
            code=final_code, artifact=path, message=message,
            action=ACTION_RUN_REGRESSION,
        )
        return

    if status == "fail":
        col.fail(
            code="regression_failed",
            artifact=path,
            message="Regression suite ran and reported test failures.",
            action=ACTION_FIX_REGRESSION,
            details={"failures": (data.get("failures") or [])[:5]},
        )


def _check_runtime_evidence(col: _Collector, subject: str, criteria: str) -> None:
    """Wave runtime results — CHECKPOINT WAVES ONLY.

    The signed authority for runtime/system validation. Must record the current
    subject AND the current ``runtime_criteria_sha256``.

    Gated on ``is_checkpoint_wave`` the same way ``_check_regression_evidence``
    is gated on ``is_last_wave``: the system checkpoint fires on odd 0-indexed
    waves plus the last, so a non-checkpoint wave produces no runtime evidence
    and must not be asked for it. The orchestrator and the merge gate read the
    SAME helper — one layer demanding evidence every wave while another gates is
    the silent-hang failure mode this cadence has to avoid.

    No ``reverify_command`` is supplied on purpose. Runtime evidence has no
    replayable producer: the aah-runtime-validator agent holds the results blob
    that ``write_runtime_results`` signs, so a missing, unverifiable, or
    stale-secret payload must be reproduced by re-dispatching the runtime
    validator, never re-signed with a new key.
    """
    from aah.core.build.orchestrator import is_checkpoint_wave

    aah = col.project_path / ".aah"
    if not is_checkpoint_wave(aah, col.wave):
        return

    runtime_path = aah / "build" / "runtime-results" / f"wave-{col.wave}-all.json"

    data = col.read(
        runtime_path,
        RUNTIME_RESULTS_PREFIXES,
        code_missing="runtime_missing",
        action_missing=ACTION_RUN_SYSTEM_CHECKPOINT,
        message_missing=(
            "No verified runtime-validation evidence for this wave. Dispatch the "
            "system checkpoint."
        ),
    )
    if data is not None:
        _check_runtime_bindings(col, runtime_path, data, subject, criteria)
        if not data.get("overall_passed"):
            col.fail(
                code="runtime_failed",
                artifact=runtime_path,
                message="Runtime validation reports overall_passed=false.",
                action=ACTION_FIX_RUNTIME,
                details={
                    "failures": {
                        k: v for k, v in (data.get("checks") or {}).items()
                        if isinstance(v, dict) and not v.get("passed")
                    },
                    "fix_category": data.get("fix_category", "build"),
                },
            )


def _check_runtime_bindings(
    col: _Collector, path: Path, data: dict, subject: str, criteria: str,
) -> None:
    """Both identity fields must match. Missing means stale, not exempt —
    artifacts predating runtime_criteria_sha256 are regenerated, not grandfathered.
    """
    for recorded, expected, code, what in (
        ((data.get("subject") or {}).get("commit_sha"), subject,
         "runtime_stale_subject", "subject"),
        (data.get("runtime_criteria_sha256"), criteria,
         "runtime_criteria_drift",
         "runtime criteria (checkpoint config, smoke steps, runtime profile, "
         "or runtime invocation)"),
    ):
        if recorded != expected:
            col.fail(
                code=code,
                artifact=path,
                message=(
                    f"{path.name} was produced against a different {what} "
                    f"(recorded {recorded}, current {expected}). Re-run it."
                ),
                action=ACTION_RUN_SYSTEM_CHECKPOINT,
            )


def _check_user_review(col: _Collector) -> None:
    """Wave user-review checkpoint — CHECKPOINT WAVES ONLY.

    Gated on ``is_checkpoint_wave`` like ``_check_runtime_evidence``: a pre-5.1.1
    config lists a UCR after EVERY wave, and demanding an approval the
    orchestrator — gated on the same helper — never requests is unresolvable.

    The approval file is written unsigned by ``validate_checkpoint approve``.
    Hashing it is ONLY a race check; this neither strengthens nor redesigns
    approval authentication, which is explicitly out of scope.
    """
    from aah.core.build.orchestrator import is_checkpoint_wave

    aah = col.project_path / ".aah"
    if not is_checkpoint_wave(aah, col.wave):
        return

    config_path = aah / "plan" / "checkpoint-config.yaml"
    if not config_path.exists():
        return
    try:
        config = read_yaml(config_path) or {}
    except Exception as exc:
        raise VerificationSystemError(
            f"malformed checkpoint-config.yaml: {exc}"
        ) from exc
    cfg = config.get("checkpoint_configuration", config)
    ucrs = (cfg.get("user_review_checkpoints") or []) if isinstance(cfg, dict) else []
    if not any(isinstance(u, dict) and u.get("after_wave") == col.wave for u in ucrs):
        return

    approval_path = (
        aah / "build" / "checkpoint-results" / f"wave-{col.wave}-approval.json"
    )
    approved = False
    if approval_path.exists():
        col.record_file(approval_path)
        try:
            approved = bool(read_json(approval_path).get("approved"))
        except Exception:
            approved = False
    if not approved:
        col.fail(
            code="user_review_pending",
            artifact=approval_path,
            message=(
                f"Wave {col.wave} has a configured user-review checkpoint with no "
                "recorded approval."
            ),
            action=ACTION_USER_REVIEW,
            details={
                "approve_command":
                    f"aah run core.build.validate_checkpoint approve --wave {col.wave}",
            },
        )


def _check_completion(col: _Collector) -> None:
    """Wave summary presence only.

    The summary is mandatory for advancement but is NOT recorded in the
    artifact hash set: it is workflow bookkeeping, not proof that the product
    works, and hashing it made regenerating a summary drift the report
    (artifact_changed) and refire technical checks.

    Expertise is not verified here at all — see
    orchestrator._check_expertise_update_wave, which attempts it once per
    product identity and records a warning instead of blocking.
    """
    summary_path = (
        col.project_path / ".aah" / "build" / "wave-summaries"
        / f"wave-{col.wave}-summary.md"
    )
    if not summary_path.exists():
        col.fail(
            code="wave_summary_missing",
            artifact=summary_path,
            message=f"Wave {col.wave} summary has not been generated.",
            action=ACTION_WAVE_SUMMARY,
            details={"command": f"aah run core.build.wave_summary --wave {col.wave}"},
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def verify_wave_evidence(project_path: Path, wave: int) -> VerificationReport:
    """Verify the complete required evidence set for ``wave``.

    Inspects everything before deciding — an operator sees every defect in one
    pass rather than fixing them one refire at a time.

    Returns a passing report when no failures exist. Raises
    ``WaveVerificationFailed(report)`` on expected evidence failures, and
    ``VerificationSystemError`` only when the project cannot be evaluated
    reliably.
    """
    project_path = Path(project_path)
    if not (project_path / ".aah").is_dir():
        raise VerificationSystemError(
            f"{project_path} is not an AAH project (no .aah/ directory)"
        )

    col = _Collector(project_path, wave)

    branch = _check_branch(col)
    if branch is None:
        # Every subsequent read would consult the wrong tree; the branch failure
        # is the only actionable one.
        raise WaveVerificationFailed(_build_report(col, subject="", criteria=""))

    subject = subject_identity(project_path)
    if subject is None:
        raise VerificationSystemError(
            f"could not resolve the subject identity of {branch}"
        )
    criteria = runtime_criteria_identity(project_path, wave)

    try:
        feature_ids = load_wave_feature_ids(project_path, wave)
    except Exception as exc:
        raise VerificationSystemError(
            f"could not resolve the feature IDs for wave {wave}: {exc}"
        ) from exc

    _check_features(col, feature_ids)
    _check_profile_catalog(col)
    _check_project_standards(col, subject)
    _check_regression_evidence(col, subject, branch)
    _check_runtime_evidence(col, subject, criteria)
    _check_completion(col)
    _check_user_review(col)

    # Race guard: nothing may have moved while we were reading. A pass over a
    # subject that changed mid-scan proves nothing, so drift can only ever ADD a
    # failure — it can never turn a failure into a pass.
    current, reason = verification_report_is_current(
        project_path, _build_report(col, subject=subject, criteria=criteria)
    )
    if not current:
        col.fail(
            code="evidence_changed_during_verification",
            artifact="<scan>",
            message=(
                f"Evidence or subject changed while verification was running "
                f"({reason}). Re-run verification against a stable tree."
            ),
            action=ACTION_BLOCKED,
            details={"reason": reason},
        )

    report = _build_report(col, subject=subject, criteria=criteria)
    if not report.passed:
        raise WaveVerificationFailed(report)
    return report


def _build_report(col: _Collector, *, subject: str, criteria: str) -> VerificationReport:
    failures = tuple(sorted(col.failures, key=verification_failure_sort_key))
    return VerificationReport(
        wave=col.wave,
        passed=not failures,
        failures=failures,
        subject_identity=subject,
        runtime_criteria_sha256=criteria,
        artifact_sha256=dict(col.artifacts),
    )

# ---------------------------------------------------------------------------
# Manual CLI — operators and focused tests only
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only wave evidence verifier")
    parser.add_argument("--wave", type=int, required=True)
    parser.add_argument("--project-path", type=Path, default=None)
    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(args.project_path)

    try:
        report = verify_wave_evidence(project_path, args.wave)
    except WaveVerificationFailed as exc:
        # Expected failures print the structured report, never a traceback.
        json.dump(exc.report.to_dict(), sys.stdout, indent=2)
        print()
        print(
            f"verify: wave {exc.report.wave} FAILED "
            f"({len(exc.report.failures)} evidence failure(s))",
            file=sys.stderr,
        )
        for failure in exc.report.failures:
            fid = f" [{failure.feature_id}]" if failure.feature_id else ""
            print(
                f"  {failure.code}{fid} → {failure.action}: {failure.message}",
                file=sys.stderr,
            )
        sys.exit(1)
    except VerificationSystemError as exc:
        print(f"verify: system error: {exc}", file=sys.stderr)
        sys.exit(2)

    json.dump(report.to_dict(), sys.stdout, indent=2)
    print()
    print(f"verify: wave {report.wave} PASSED", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
