"""Shared evidence readers and pure verdict predicates for wave verification."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

from aah.core.build.qa_evidence import (
    latest_attempt,
    latest_human_decision,
    profile_snapshot_for_subject,
)
from aah.core.build.verification_identity import subject_identity
from aah.core.common.git_utils import (
    GitError,
    current_branch,
    is_clean_ignoring,
    list_worktrees,
)
from aah.core.common.io_utils import read_yaml
from aah.core.common.verified_artifacts import ArtifactState, load_attested_artifact


REGRESSION_PREFIX = ["aah", "run", "core.build.run_regression_suite"]
FEATURE_TEST_PREFIX = ["aah", "run", "core.build.run_feature_tests"]
QA_REPORT_PREFIX = ["aah", "run", "core.build.write_qa_report"]
QUALITY_PREFIX = ["aah", "run", "core.build.quality_checks"]
RUNTIME_RESULTS_PREFIX = ["aah", "run", "core.build.write_runtime_results"]
RUNTIME_RESULTS_PREFIXES = [
    RUNTIME_RESULTS_PREFIX,
    ["aah", "run", "core.build.verify"],
]
NO_VERDICT_STATUSES = ("no_signal", "skipped_wrong_branch")


def read_attested(
    path: Path,
    project_path: Path,
    expected_command_prefix: list[str] | list[list[str]],
) -> dict | None:
    """Return a verified payload, logging every rejected artifact state."""
    artifact = load_attested_artifact(
        path, project_path, expected_command_prefix, "json"
    )
    if artifact.state is ArtifactState.VERIFIED:
        return artifact.payload
    if artifact.state is ArtifactState.MISSING:
        try:
            branch = current_branch(cwd=project_path)
        except GitError:
            branch = "unknown"
        print(
            f"[verify] Evidence not found at {path} (branch={branch!r}). "
            "Treating as not-yet-run.",
            file=sys.stderr,
        )
    else:
        print(
            f"[verify] Verification failed for {path}: {artifact.reason}. "
            "Treating as not-yet-run.",
            file=sys.stderr,
        )
    return None

def feature_evidence_freshness_problem(
    data: Mapping[str, Any],
    *,
    project_path: Path,
    subject_path: Path,
    feature_id: str,
    expected_branch: str,
    expected_sha: str,
) -> str | None:
    from aah.core.build.validate_implementation import (
        _feature_subject_evidence_problem,
    )

    return _feature_subject_evidence_problem(
        dict(data),
        project_path=project_path,
        subject_path=subject_path,
        feature_id=feature_id,
        expected_branch=expected_branch,
        expected_sha=expected_sha,
    )


def read_fresh_feature_evidence(
    path: Path,
    project_path: Path,
    expected_command_prefix: list[str],
    *,
    subject_path: Path,
    feature_id: str,
    expected_branch: str,
    expected_sha: str,
) -> dict | None:
    """Return feature evidence only when attested and current."""
    data = read_attested(path, project_path, expected_command_prefix)
    if data is None:
        return None
    reason = feature_evidence_freshness_problem(
        data,
        project_path=project_path,
        subject_path=subject_path,
        feature_id=feature_id,
        expected_branch=expected_branch,
        expected_sha=expected_sha,
    )
    if reason is None:
        return data
    print(
        f"[verify] Freshness check failed for {path}: {reason}. Treating as stale.",
        file=sys.stderr,
    )
    return None


def resolve_feature_subject(
    project_path: Path, feature_id: str, total_features_in_wave: int
) -> dict[str, Any]:
    """Resolve the clean project or worktree subject for one feature."""
    worktree = (project_path / ".claude" / "worktrees" / feature_id).resolve()
    expected_branch = f"feature/{feature_id}"
    try:
        registered = {
            Path(item["path"]).resolve()
            for item in list_worktrees(cwd=project_path)
            if item.get("path")
        }
    except GitError as exc:
        return {"ok": False, "reason": f"Failed to inspect worktrees: {exc}"}

    if worktree not in registered:
        if total_features_in_wave <= 1 and not worktree.exists():
            try:
                branch = current_branch(cwd=project_path)
                sha = subject_identity(project_path)
            except GitError as exc:
                return {"ok": False, "reason": f"Failed to read project root: {exc}"}
            if sha is None:
                return {"ok": False, "reason": "Project root has no commits"}
            return {
                "ok": True,
                "subject_path": str(project_path),
                "subject_branch": branch,
                "subject_sha": sha,
            }
        return {
            "ok": False,
            "reason": f"Worktree {worktree} is not a registered git worktree",
        }

    try:
        actual_branch = current_branch(cwd=worktree)
        if actual_branch != expected_branch:
            return {
                "ok": False,
                "reason": f"Worktree branch is {actual_branch}, expected {expected_branch}",
            }
        if not is_clean_ignoring(cwd=worktree):
            return {"ok": False, "reason": "Worktree has uncommitted changes (dirty)"}
        sha = subject_identity(worktree)
    except GitError as exc:
        return {"ok": False, "reason": f"Failed to inspect worktree: {exc}"}
    if sha is None:
        return {"ok": False, "reason": "Worktree has no commits"}
    return {
        "ok": True,
        "subject_path": str(worktree),
        "subject_branch": expected_branch,
        "subject_sha": sha,
    }


def regression_evidence_problem(
    data: Mapping[str, Any], expected_branch: str, expected_subject: str
) -> tuple[str, str] | None:
    recorded = data.get("subject")
    if not isinstance(recorded, dict):
        return "subject_block_missing", "Regression evidence carries no subject binding."
    if recorded.get("branch") != expected_branch:
        return (
            "subject_branch_mismatch",
            f"Regression evidence records a different branch, expected {expected_branch!r}.",
        )
    if recorded.get("commit_sha") != expected_subject:
        return (
            "subject_sha_stale",
            "Regression evidence is bound to a subject that is no longer current. "
            "Re-run regression on the current integration tree.",
        )
    return None


def read_regression_evidence(
    aah_path: Path, project_path: Path, wave: int
) -> tuple[dict | None, str]:
    path = aah_path / "build" / "test-results" / "regression-latest.json"
    artifact = load_attested_artifact(
        path, project_path, REGRESSION_PREFIX, "json"
    )
    if artifact.state is not ArtifactState.VERIFIED:
        return None, artifact.reason or artifact.state.value
    data = artifact.payload
    assert data is not None
    branch = f"integration/wave-{wave}"
    current_subject = subject_identity(project_path, ref=branch)
    if current_subject is None:
        return None, "current_subject_unresolvable"
    problem = regression_evidence_problem(data, branch, current_subject)
    return (None, problem[0]) if problem else (data, "")


def verify_build_evidence(
    project_path: Path, build_branch: str
) -> tuple[str, str] | None:
    """Wave-free promotion gate for the lean, sequential build.

    Verifies the cumulative regression evidence, which must:
      - exist as attested evidence (signed, untampered),
      - have rendered a verdict (not no_signal / wrong-branch),
      - bind to the current tree identity of ``build_branch``,
      - be passing.

    Standards is deliberately NOT verified here. The build skill runs the
    standards gate itself and loops on its exit code (fail -> ``aah-fix`` ->
    re-run), which is the enforcing control. Re-checking the artifact at promote
    added nothing on a single-branch build, where the code is already on the
    develop branch before promotion runs. Runtime validation likewise stays out:
    advisory, resolved at the per-module checkpoint.

    Returns ``None`` when promotion may proceed, else ``(code, message)``.
    """
    path = project_path / ".aah" / "build" / "test-results" / "regression-latest.json"
    data = read_attested(path, project_path, REGRESSION_PREFIX)
    if data is None:
        return (
            "regression_missing",
            "No verified regression evidence. Run the regression suite on the "
            "build branch before promoting.",
        )
    status = data.get("status", "pass" if data.get("passed") else "fail")
    if status in NO_VERDICT_STATUSES:
        return (
            "regression_no_signal",
            "Regression rendered no verdict "
            f"({data.get('signal_reason') or status}): "
            f"{data.get('message', 'no detail recorded')}",
        )
    current_subject = subject_identity(project_path, ref=build_branch)
    if current_subject is None:
        return ("build_branch_unresolvable", f"Cannot resolve {build_branch!r} tip.")
    problem = regression_evidence_problem(data, build_branch, current_subject)
    if problem:
        return problem
    if status == "fail":
        return ("regression_failed", "Regression suite ran and reported test failures.")
    return None


def standards_evidence_passed(data: Mapping[str, Any]) -> bool:
    schema = data.get("schema_version")
    known_schema = schema is None or (
        isinstance(schema, int)
        and not isinstance(schema, bool)
        and schema in (1, 2)
    )
    return bool(
        known_schema
        and data.get("overall_passed") is True
        and data.get("verdict") == "PASS"
    )


def feature_qa_state_validation(
    project_path: Path, feature_id: str, expected_subject_sha: str
) -> tuple[dict[str, Any], str | None]:
    from aah.core.build.qa_evidence import derive_qa_state

    state = derive_qa_state(project_path, feature_id)
    if state.get("current_attempt") is None:
        return state, "qa_attempt_missing"
    if state.get("current_subject_sha") != expected_subject_sha:
        return state, "qa_attempt_stale_subject"
    return state, None


def feature_qa_approval_validation(
    project_path: Path, feature_id: str, expected_subject_sha: str
) -> tuple[dict | None, str | None]:
    """Validate the final QA decision, including profile and human approval."""
    attempt = latest_attempt(project_path, feature_id)
    if attempt is None:
        return None, "qa_attempt_missing"

    recorded_sha = (attempt.get("subject") or {}).get("commit_sha")
    if recorded_sha != expected_subject_sha:
        return attempt, "qa_attempt_stale_subject"

    _snapshot, snapshot_problem = profile_snapshot_for_subject(
        project_path, feature_id, expected_subject_sha
    )
    if snapshot_problem.startswith("qa_profile_"):
        return attempt, snapshot_problem

    verdict = str(attempt.get("verdict") or "").lower()
    if verdict == "pass":
        return attempt, None

    human = latest_human_decision(
        project_path, feature_id, attempt.get("attempt")
    )
    if human is not None and human.get("decision") == "approve":
        return attempt, None
    if human is not None and human.get("decision") == "request_rework":
        return attempt, "qa_human_rework_requested"
    return attempt, f"qa_verdict_{verdict or 'missing'}"


def feature_has_passed_attested_tests(aah_path: Path, feature_id: str) -> bool:
    path = aah_path / "build" / "test-results" / f"{feature_id}.json"
    data = read_attested(path, aah_path.parent, FEATURE_TEST_PREFIX)
    return data is not None and bool(data.get("passed"))


def domain_files_problem(domains_dir: Path) -> str | None:
    if not domains_dir.is_dir():
        return "directory missing"
    yaml_files = sorted(domains_dir.glob("*.yaml"))
    if not yaml_files:
        return "no domain files"
    for path in yaml_files:
        try:
            if path.stat().st_size == 0:
                return f"empty file: {path.name}"
            data = read_yaml(path)
        except OSError:
            return f"unreadable: {path.name}"
        except Exception:
            return f"unparseable: {path.name}"
        if not isinstance(data, dict) or not data:
            return f"empty mapping: {path.name}"
    return None


def expertise_marker_fresh(
    marker: Path, project_path: Path, wave: int
) -> tuple[bool, str]:
    from aah.core.build.wave_markers import is_expertise_marker_fresh

    return is_expertise_marker_fresh(
        marker, project_path, integration_branch=f"integration/wave-{wave}"
    )


def expertise_artifacts_problem(aah_path: Path) -> tuple[str, str] | None:
    expertise_path = aah_path / "codebase-intel" / "expertise.yaml"
    if not expertise_path.exists():
        return "expertise_yaml_missing", "expertise.yaml is missing"
    try:
        expertise = read_yaml(expertise_path) or {}
        style_guide = (expertise.get("consumption_views") or {}).get("style_guide", "")
    except Exception:
        style_guide = ""
    if not str(style_guide).strip():
        return "expertise_style_guide_empty", "consumption_views.style_guide is empty"
    domains_problem = domain_files_problem(aah_path / "codebase-intel" / "domains")
    if domains_problem is not None:
        return "expertise_domains_invalid", domains_problem
    return None
