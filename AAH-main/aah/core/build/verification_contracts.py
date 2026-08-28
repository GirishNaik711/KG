"""Result and remediation contracts for read-only wave verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


ACTION_BLOCKED = "blocked"
ACTION_NO_SIGNAL = "no_signal"
ACTION_GENERATE_ARTIFACTS = "generate_artifacts"
ACTION_RUN_FEATURE_TESTS = "run_feature_tests"
ACTION_RUN_QA = "run_qa"
ACTION_HUMAN_REVIEW = "human_review_required"
ACTION_USER_REVIEW = "user_review"
# Standards is PROJECT-scoped and runs once, in the last wave before regression
# — there is no per-feature standards action. ACTION_RUN_STANDARDS is retained
# only as the name of the whole-codebase run.
ACTION_RUN_STANDARDS = "run_project_standards"
ACTION_FIX_STANDARDS = "fix_standards"
ACTION_REVERIFY = "reverify_evidence"
ACTION_RUN_REGRESSION = "run_regression"
ACTION_FIX_REGRESSION = "fix_regression"
ACTION_RUN_SYSTEM_CHECKPOINT = "run_system_checkpoint"
ACTION_FIX_RUNTIME = "fix_runtime_validation"
ACTION_WAVE_SUMMARY = "generate_wave_summary"
ACTION_UPDATE_EXPERTISE = "update_expertise"


_ACTION_ORDER = (
    ACTION_BLOCKED,
    ACTION_REVERIFY,
    ACTION_GENERATE_ARTIFACTS,
    ACTION_RUN_FEATURE_TESTS,
    ACTION_RUN_QA,
    ACTION_HUMAN_REVIEW,
    ACTION_RUN_STANDARDS,
    ACTION_FIX_STANDARDS,
    ACTION_RUN_REGRESSION,
    ACTION_FIX_REGRESSION,
    ACTION_NO_SIGNAL,
    ACTION_RUN_SYSTEM_CHECKPOINT,
    ACTION_FIX_RUNTIME,
    ACTION_WAVE_SUMMARY,
    ACTION_USER_REVIEW,
    ACTION_UPDATE_EXPERTISE,
)
_PRIORITY = {name: index for index, name in enumerate(_ACTION_ORDER)}


@dataclass(frozen=True)
class VerificationFailure:
    code: str
    artifact: str
    message: str
    action: str
    wave: int
    feature_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        result = {
            "code": self.code,
            "artifact": self.artifact,
            "message": self.message,
            "action": self.action,
            "wave": self.wave,
        }
        if self.feature_id:
            result["feature_id"] = self.feature_id
        if self.details:
            result["details"] = dict(self.details)
        return result


@dataclass(frozen=True)
class VerificationReport:
    wave: int
    passed: bool
    failures: tuple[VerificationFailure, ...]
    subject_identity: str
    runtime_criteria_sha256: str
    artifact_sha256: Mapping[str, str]

    def to_dict(self) -> dict:
        return {
            "wave": self.wave,
            "passed": self.passed,
            "failures": [failure.to_dict() for failure in self.failures],
            "subject_identity": self.subject_identity,
            "runtime_criteria_sha256": self.runtime_criteria_sha256,
            "artifact_sha256": dict(self.artifact_sha256),
        }


class WaveVerificationFailed(Exception):
    """Expected evidence failures, carrying the complete report."""

    def __init__(self, report: VerificationReport):
        first = report.failures[0].code if report.failures else "none"
        super().__init__(
            f"wave {report.wave} verification failed: "
            f"{len(report.failures)} evidence failure(s); first={first}"
        )
        self.report = report


class VerificationSystemError(Exception):
    """Verification could not evaluate required project state reliably."""


def verification_failure_sort_key(failure: VerificationFailure) -> tuple:
    return (
        _PRIORITY.get(failure.action, 999),
        failure.action,
        failure.feature_id or "",
        failure.artifact,
    )


def action_from_verification_failure(
    first: VerificationFailure,
    failures: tuple[VerificationFailure, ...] | list[VerificationFailure],
    *,
    source_integration_sha: str | None = None,
) -> dict:
    """Build one dispatch action while retaining the complete failure list."""
    action: dict[str, Any] = {
        "action": first.action,
        "wave": first.wave,
        "reason": first.message,
        "verification_failures": [failure.to_dict() for failure in failures],
    }
    details = dict(first.details)

    if first.action == ACTION_GENERATE_ARTIFACTS:
        action["missing"] = [
            {"feature_id": f.feature_id, "artifact": f.artifact, "code": f.code}
            for f in failures
            if f.action == ACTION_GENERATE_ARTIFACTS
        ]
        action["generate_actions"] = [
            {
                "action": ACTION_RUN_FEATURE_TESTS,
                "feature_id": f.feature_id,
                **({"command": f.details["command"]} if f.details.get("command") else {}),
            }
            for f in failures
            if f.action == ACTION_GENERATE_ARTIFACTS and f.feature_id
        ]
    elif first.action == ACTION_RUN_QA:
        action["features"] = sorted({
            f.feature_id
            for f in failures
            if f.action == first.action and f.feature_id
        })
        action["subject_map"] = {
            f.feature_id: dict(f.details["subject"])
            for f in failures
            if (
                f.action == first.action
                and f.feature_id
                and isinstance(f.details.get("subject"), Mapping)
            )
        }
    elif first.action == ACTION_HUMAN_REVIEW and first.feature_id:
        # Import lazily: qa_routing imports verify, which imports this module.
        # The command strings remain owned by qa_routing instead of drifting
        # between normal orchestration and promotion-time remediation.
        from aah.core.build.qa_routing import feature_qa_review_commands

        action.update(feature_qa_review_commands(first.feature_id))
    elif first.action == ACTION_FIX_STANDARDS:
        action.update(
            route="aah-fix",
            repair_mode="current_wave_repair",
            source_artifact=first.artifact,
            failures=details.get("failures", {}),
        )
        if details.get("source_integration_sha"):
            action["source_integration_sha"] = details["source_integration_sha"]
    elif first.action == ACTION_NO_SIGNAL:
        action["signal_reason"] = details.get("signal_reason", first.code)
    elif first.action == ACTION_REVERIFY:
        action["evidence_type"] = first.code
    elif first.action == ACTION_FIX_REGRESSION:
        action.update(
            failures=details.get("failures", []),
            route="aah-fix",
            repair_mode="current_wave_repair",
            source_artifact=first.artifact,
        )
    elif first.action == ACTION_FIX_RUNTIME:
        action.update(
            failures=details.get("failures", {}),
            route="aah-fix",
            repair_mode="current_wave_repair",
            source_artifact=first.artifact,
        )
    elif first.action == ACTION_RUN_SYSTEM_CHECKPOINT:
        action["keep_running"] = True

    if first.action in {
        ACTION_FIX_STANDARDS, ACTION_FIX_REGRESSION, ACTION_FIX_RUNTIME,
    } and source_integration_sha:
        action["source_integration_sha"] = source_integration_sha

    if first.feature_id:
        action.setdefault("feature_id", first.feature_id)
    for key in (
        "command", "skill", "approve_command", "request_rework_command",
    ):
        if key in details:
            action.setdefault(key, details[key])
    return action
