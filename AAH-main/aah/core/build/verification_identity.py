"""Subject, criteria, and snapshot identities used by wave verification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from aah.core.build.verification_contracts import (
    VerificationReport,
    VerificationSystemError,
)
from aah.core.build.verification_profiles import profile_hash
from aah.core.common.git_utils import (
    GitError,
    code_subject_identity,
    current_branch,
)
from aah.core.common.io_utils import read_json, read_yaml
from aah.core.common.readiness import load_runtime_resources, resolve_runtime_profile


REQUIRED_CHECK_STEP_KEY = {
    "build": "build",
    "boot": "server_start",
    "smoke": "smoke_tests",
    "cloud_readiness": "cloud_readiness",
}


def subject_identity(project_path: Path, ref: str = "HEAD") -> str | None:
    """Return the code identity, excluding framework bookkeeping."""
    try:
        return code_subject_identity(cwd=project_path, ref=ref)
    except GitError:
        return None


def resolve_runtime_verification_profile(
    project_path: Path,
) -> tuple[dict | None, str]:
    """Resolve the runtime profile or return a stable refusal reason."""
    aah_path = project_path / ".aah"
    registry_path = aah_path / "decision-registry.yaml"
    context: dict = {}
    if registry_path.exists():
        try:
            registry = read_yaml(registry_path) or {}
        except Exception:
            return None, "malformed_decision_registry"
        context = registry.get("context", {}) if isinstance(registry, dict) else {}
    try:
        resources = load_runtime_resources(aah_path)
    except Exception:
        resources = None
    try:
        return resolve_runtime_profile(aah_path, context, resources), ""
    except ValueError:
        return None, "missing_topology"


def runtime_profile_evidence_validation(
    project_path: Path, runtime_data: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate runtime-profile bindings without applying rollout policy."""
    from aah.core.common import runtime_profile

    profile, profile_reason = resolve_runtime_verification_profile(project_path)
    confirmation = runtime_profile.read_confirmed(project_path)
    problems: list[str] = []
    evidence = runtime_data.get("runtime_evidence")
    if not isinstance(evidence, dict):
        problems.append("runtime_evidence_missing")
        evidence = {}
    if profile is None:
        problems.append(f"profile_unresolved:{profile_reason}")
    if confirmation.get("result") != "confirmed":
        problems.append(f"profile_not_confirmed:{confirmation.get('reason')}")
    current_subject = subject_identity(project_path)
    if current_subject is None or evidence.get("integration_sha") != current_subject:
        problems.append("integration_sha_drift")

    required = list(profile.get("required_checks") or []) if profile else []
    if profile:
        bindings = evidence.get("profile_bindings") or {}
        if bindings.get("profile_hash") != profile.get("profile_hash"):
            problems.append("profile_hash_drift")
        if set(evidence.get("required_checks") or []) != set(required):
            problems.append("required_checks_drift")
    checks = runtime_data.get("checks") or {}
    for required_check in required:
        step = checks.get(REQUIRED_CHECK_STEP_KEY.get(required_check, required_check))
        if not isinstance(step, dict) or step.get("passed") is not True:
            problems.append(f"required_check_absent_or_failed:{required_check}")
    return {"problems": problems, "required_checks": required}


def _read_criteria_mapping(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = read_yaml(path)
    except Exception as exc:
        raise VerificationSystemError(
            f"malformed runtime criteria file {path}: {exc}"
        ) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise VerificationSystemError(f"runtime criteria file {path} is not a mapping")
    return data


def runtime_criteria_identity(project_path: Path, wave: int) -> str:
    """Hash the semantic runtime criteria for one wave, excluding timestamps."""
    aah = project_path / ".aah"
    cfg = _read_criteria_mapping(aah / "plan" / "checkpoint-config.yaml").get(
        "checkpoint_configuration", {}
    )
    if not isinstance(cfg, dict):
        cfg = {}
    smoke = _read_criteria_mapping(
        aah / "plan" / "smoke-tests" / f"wave-{wave}.yaml"
    )
    manifest = _read_criteria_mapping(aah / "manifest.yaml")
    profile, _reason = resolve_runtime_verification_profile(project_path)
    return profile_hash({
        "system_checkpoints": cfg.get("system_checkpoints"),
        "verification_profiles": cfg.get("verification_profiles"),
        "user_review_checkpoints": cfg.get("user_review_checkpoints"),
        "smoke_steps": smoke.get("steps"),
        "runtime_profile": {
            key: value for key, value in (profile or {}).items()
            if key not in ("generated_at", "confirmed_at")
        },
        "runtime_invocation": {
            key: manifest.get(key)
            for key in ("stack_choices", "start_command", "port")
        },
    })


def canonical_payload_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def verification_report_is_current(
    project_path: Path, report: VerificationReport
) -> tuple[bool, str]:
    """Recheck the in-memory verification snapshot before promotion."""
    project_path = Path(project_path)
    try:
        if current_branch(cwd=project_path) != f"integration/wave-{report.wave}":
            return False, "branch_changed"
    except GitError as exc:
        return False, f"branch_unreadable:{exc}"
    if report.subject_identity:
        if subject_identity(project_path) != report.subject_identity:
            return False, "subject_changed"
    if report.runtime_criteria_sha256:
        try:
            current = runtime_criteria_identity(project_path, report.wave)
        except VerificationSystemError:
            return False, "runtime_criteria_unreadable"
        if current != report.runtime_criteria_sha256:
            return False, "runtime_criteria_changed"
    for relative_path, expected in report.artifact_sha256.items():
        path = project_path / relative_path
        if not path.exists():
            return False, f"artifact_removed:{relative_path}"
        if file_sha256(path) == expected:
            continue
        try:
            if canonical_payload_sha256(read_json(path)) == expected:
                continue
        except Exception:
            pass
        return False, f"artifact_changed:{relative_path}"
    return True, ""
