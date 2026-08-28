#!/usr/bin/env python3
"""Automated code quality validation.

The standards gate runs ONCE per project — after the last module, before
regression — over the whole codebase (``run-project``), and over EVERY package
root in it: discovery is per package (``discover_package_roots``), not one
adapter for the whole tree, so ``backend/`` and ``frontend/`` each get their own
linter instead of the frontend being silently skipped.

It is not once per feature. Per-feature scoping was always fiction:
``lint_command`` returns ``ruff check <root>`` and
``static_analysis_command`` returns ``bandit -r <root>``, so the "per-feature"
gate scanned the entire tree and attributed every finding to whichever feature
triggered it. A feature's ``file_scope`` reached the evidence as scope metadata
and was never passed to either tool.

Relocating also made the check possible at all: per-feature evidence is bound to
a ``.claude/worktrees/<FID>`` subject that ``promote_to_develop`` deletes after
each wave, so a deferred per-feature run has no subject to bind to.

Lint and static analysis run. Coverage and type-check are disabled and recorded
as explicit ``not_applicable`` skips — see ``DISABLED_CHECKS``.

Usage:
    aah run core.build.quality_checks run-project --project-path . --subject-branch build/x
    aah run core.build.quality_checks run-project --project-path . --wave 3
    aah run core.build.quality_checks linting --project-path . --feature-id F001
    aah run core.build.quality_checks static-analysis --project-path . --feature-id F001
    aah run core.build.quality_checks summary --project-path . --feature-id F001
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from aah.core.build.evidence import (
    EvidenceError,
    assert_subject_unchanged,
    build_evidence_v2_record,
    capture_subject,
    hash_feature_contract,
    hash_test_inputs,
)
from aah.core.build.lang_checks import PACKAGE_FINGERPRINTS
from aah.core.common.attestation import write_attested
from aah.core.common.execution import CommandOutcome, CommandSpec, run_bounded_command
from aah.core.common.feature_utils import find_feature_file, load_feature_data
from aah.core.common.io_utils import read_json, read_yaml, write_json


# Canonical command prefix recorded in the attestation block. The
# orchestrator's _features_needing_standards_check() reader uses this
# prefix in expected_command_prefix to confirm the file came from this writer.
COMMAND_PREFIX = ["aah", "run", "core.build.quality_checks"]

# Canonical status vocabulary for quality-check results. Mirrors
# evidence.EVIDENCE_STATUSES but defined locally (no cross-module import).
QUALITY_STATUSES = frozenset({"pass", "fail", "no_signal", "not_applicable"})


@dataclass(frozen=True)
class QualityTarget:
    """One language/package root touched by a feature contract."""

    root: Path
    stack: str
    files: tuple[str, ...]


#: Alias of the canonical fingerprint set in the adapter registry — one list, so
#: feature-scope resolution and package discovery cannot drift apart.
_QUALITY_FINGERPRINTS = PACKAGE_FINGERPRINTS


def _nearest_quality_target(subject_path: Path, relative_file: str) -> tuple[Path, str]:
    """Resolve a file to its nearest language package root."""
    subject_path = subject_path.resolve()
    candidate = (subject_path / relative_file).resolve()
    if not candidate.is_relative_to(subject_path):
        raise EvidenceError(f"Feature file_scope escapes subject: {relative_file}")
    current = candidate if candidate.is_dir() else candidate.parent

    from aah.core.build.lang_checks import detect

    while current.is_relative_to(subject_path):
        if any((current / name).exists() for name in _QUALITY_FINGERPRINTS):
            adapter = detect(current, manifest={})
            if adapter.name != "generic":
                return current, adapter.name
        if current == subject_path:
            break
        current = current.parent

    adapter = detect(subject_path)
    return subject_path, adapter.name


def resolve_quality_targets(
    project_path: Path,
    subject_path: Path,
    feature_id: str,
) -> list[QualityTarget]:
    """Resolve all package roots implicated by a feature's ``file_scope``.

    Feature scope chooses the adapter; repository-level first-match detection
    is only the compatibility fallback for legacy contracts with no scope.
    """
    subject_path = subject_path.resolve()
    features_dir = subject_path / ".aah" / "plan" / "features"
    feature = load_feature_data(features_dir, feature_id)
    if feature is None:
        # Direct adapter/unit use may not have an AAH plan at all. Once a plan
        # directory exists, however, silently falling back to repository-wide
        # detection for an unknown or malformed feature would run the wrong
        # stack and can turn a contract problem into a standards retry loop.
        if features_dir.is_dir():
            raise EvidenceError(f"Feature contract unavailable: {feature_id}")
        feature = {}
    file_scope = [
        str(path).strip().replace("\\", "/")
        for path in (feature.get("file_scope") or [])
        if str(path).strip()
    ]
    if not file_scope:
        from aah.core.build.lang_checks import detect

        adapter = detect(subject_path)
        return [QualityTarget(subject_path, adapter.name, ())]

    grouped: dict[tuple[Path, str], list[str]] = {}
    for relative_file in file_scope:
        root, stack = _nearest_quality_target(subject_path, relative_file)
        try:
            scoped_file = (subject_path / relative_file).resolve().relative_to(root).as_posix()
        except ValueError as exc:
            raise EvidenceError(
                f"Feature file_scope is outside resolved package root: {relative_file}"
            ) from exc
        grouped.setdefault((root, stack), []).append(scoped_file)

    return [
        QualityTarget(root, stack, tuple(sorted(set(files))))
        for (root, stack), files in sorted(
            grouped.items(), key=lambda item: item[0][0].as_posix()
        )
    ]


def _run_quality_command(
    result: dict,
    command: str,
    project_path: Path,
    *,
    timeout: int,
    timeout_message: str,
    error_label: str,
) -> CommandOutcome | None:
    """Execute one shell-based gate and populate its common detail fields."""
    outcome = run_bounded_command(
        CommandSpec(
            command,
            shell=True,
            cwd=str(project_path),
            timeout_sec=timeout,
        )
    )
    if outcome.state == "timeout":
        result["details"]["message"] = timeout_message
    elif outcome.state != "executed":
        result["details"]["message"] = f"{error_label} error: {outcome.error}"
    else:
        result["details"].update(
            exit_code=outcome.returncode,
            stdout=outcome.stdout[-3000:] if outcome.stdout else "",
            stderr=outcome.stderr[-1000:] if outcome.stderr else "",
        )
        if outcome.stdout_truncated or outcome.stderr_truncated:
            result["details"].update(
                message=f"{error_label} output exceeded the capture limit",
                blocking_reason="output_truncated",
            )
            result["passed"] = False
            result["status"] = "no_signal"
            return None
        return outcome
    result["passed"] = False
    result["status"] = "no_signal"
    return None


def _record_quality_audit(
    project_path: Path,
    *,
    gate: str,
    check: str,
    feature_id: str,
    status: str,
    reason: str,
    mode: str,
) -> None:
    """Append a quality-gate audit event to .aah/audit/quality-gate-log.json."""
    audit_dir = project_path / ".aah" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / "quality-gate-log.json"

    events = []
    if audit_path.exists():
        try:
            data = read_json(audit_path)
            events = data.get("events", [])
        except Exception:
            pass

    events.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "gate": gate,
        "check": check,
        "feature_id": feature_id,
        "status": status,
        "reason": reason,
        "mode": mode,
    })

    write_json({"events": events}, audit_path)


def _scope_metadata(target: QualityTarget, subject_path: Path) -> dict:
    return {
        "stack": target.stack,
        "root": target.root.relative_to(subject_path).as_posix()
        if target.root != subject_path else ".",
        "files": list(target.files),
    }


def _combine_target_results(
    check: str,
    feature_id: str,
    subject_path: Path,
    targets: list[QualityTarget],
    runner: Callable[[Path], dict],
) -> dict:
    """Run one check across every affected package and fold fail-closed."""
    results: list[dict] = []
    for target in targets:
        result = runner(target.root)
        result["scope"] = _scope_metadata(target, subject_path)
        results.append(result)

    if len(results) == 1:
        results[0]["details"]["scope"] = results[0]["scope"]
        return results[0]

    statuses = [str(result.get("status") or "no_signal") for result in results]
    if "no_signal" in statuses:
        status = "no_signal"
    elif "fail" in statuses:
        status = "fail"
    elif all(item == "not_applicable" for item in statuses):
        status = "not_applicable"
    else:
        status = "pass"

    violations = [
        violation
        for result in results
        for violation in result.get("violations", [])
    ]
    stdout = "\n".join(
        f"=== {result['scope']['stack']}:{result['scope']['root']} ===\n"
        f"{result.get('details', {}).get('stdout', '')}"
        for result in results
    )
    stderr = "\n".join(
        f"=== {result['scope']['stack']}:{result['scope']['root']} ===\n"
        f"{result.get('details', {}).get('stderr', '')}"
        for result in results
    )
    return {
        "check": check,
        "feature_id": feature_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "passed": all(result.get("passed") is True for result in results),
        "status": status,
        "violations": violations,
        "scopes": [result["scope"] for result in results],
        "details": {
            "message": f"{check} completed for {len(results)} package scopes",
            "stdout": stdout,
            "stderr": stderr,
            "targets": results,
            "commands": [
                result.get("details", {}).get("command") for result in results
                if result.get("details", {}).get("command")
            ],
        },
    }


# ---------------------------------------------------------------------------
# Linting
# ---------------------------------------------------------------------------


def run_linting(
    project_path: Path,
    feature_id: str,
    *,
    targets: list[QualityTarget] | None = None,
    subject_path: Path | None = None,
) -> dict:
    """Run linting against the feature's affected package roots."""
    subject_path = Path(subject_path or project_path).resolve()
    targets = targets or resolve_quality_targets(project_path, subject_path, feature_id)
    return _combine_target_results(
        "linting",
        feature_id,
        subject_path,
        targets,
        lambda root: _run_linting_for_root(project_path, feature_id, root),
    )


def _run_linting_for_root(
    project_path: Path, feature_id: str, execution_path: Path
) -> dict:
    """Run linting against one resolved language package.

    Producer contract:
        result["details"]["stdout"] and result["details"]["stderr"] are
        strings. The run-all path concatenates these into the attestation
        block; non-string values cause a coercion warning and are coerced
        loudly. If you need to surface structured data, add a sibling key
        like result["details"]["findings"] (list[dict]) — that's outside
        the attestation hash and the contract is unaffected.
    """
    result = {
        "check": "linting",
        "feature_id": feature_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "passed": True,
        "details": {},
    }

    linter_cmd = _resolve_linter(execution_path)
    if not linter_cmd:
        result["details"]["message"] = "No linter configured or detected"
        result["details"]["skip_reason"] = "no_linter"
        result["status"] = "not_applicable"
        return result

    result["details"]["command"] = linter_cmd

    proc = _run_quality_command(
        result, linter_cmd, execution_path,
        timeout=60, timeout_message="Linting timed out", error_label="Linting",
    )
    if proc is None:
        return result
    if _tool_missing(proc.returncode, proc.stderr):
        # Applicable tool is not installed → block (was: silent pass
        # via `|| true`). A skip requires the adapter to return None
        # (no tool for this stack), not a missing binary.
        result["passed"] = False
        result["status"] = "no_signal"
        result["details"]["message"] = (
            "required tool not installed (linter) — install it or "
            "configure the stack; not skipping a missing gate"
        )
        result["details"]["blocking_reason"] = "tool_not_installed"
    elif proc.returncode != 0:
        # Exit code IS the verdict. There is no parse step and no severity
        # split: the linter emits human-readable `file:line: CODE message`
        # lines, which is exactly what the repair route needs to read, and
        # details.stdout carries them verbatim. The previous code split the
        # linter's JSON by line and string-matched it, so it counted lines of
        # JSON syntax as violations and invented `critical` from whichever
        # lines happened to contain the word. Blocking on "the linter reported
        # at least one error" is less precise in name and more correct in fact.
        result["passed"] = False
        result["status"] = "fail"
        result["details"]["message"] = (
            f"Linter reported errors (exit {proc.returncode}) — see details.stdout"
        )
    else:
        result["status"] = "pass"
        result["details"]["message"] = "Linting passed"

    return result


# ---------------------------------------------------------------------------
# Static Analysis
# ---------------------------------------------------------------------------


def run_static_analysis(
    project_path: Path,
    feature_id: str,
    *,
    targets: list[QualityTarget] | None = None,
    subject_path: Path | None = None,
) -> dict:
    """Run static analysis against the affected package roots."""
    subject_path = Path(subject_path or project_path).resolve()
    targets = targets or resolve_quality_targets(project_path, subject_path, feature_id)
    return _combine_target_results(
        "static_analysis",
        feature_id,
        subject_path,
        targets,
        lambda root: _run_static_analysis_for_root(project_path, feature_id, root),
    )


def _run_static_analysis_for_root(
    project_path: Path, feature_id: str, execution_path: Path
) -> dict:
    """Run static analysis for one resolved language package.

    Producer contract: see run_linting docstring. details.stdout and
    details.stderr are strings.
    """
    result = {
        "check": "static_analysis",
        "feature_id": feature_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "passed": True,
        "violations": [],
        "details": {},
    }

    sa_cmd = _adapter_cmd_to_shell(execution_path, "static_analysis_command")
    if not sa_cmd:
        result["details"]["message"] = "No static analyzer configured or detected"
        result["details"]["skip_reason"] = "no_analyzer"
        result["status"] = "not_applicable"
        return result

    result["details"]["command"] = sa_cmd

    proc = _run_quality_command(
        result, sa_cmd, execution_path,
        timeout=120,
        timeout_message="Static analysis timed out",
        error_label="Static analysis",
    )
    if proc is None:
        return result
    if _tool_missing(proc.returncode, proc.stderr):
        result["passed"] = False
        result["status"] = "no_signal"
        result["details"]["message"] = (
            "required tool not installed (static analyzer) — install it "
            "or configure the stack; not skipping a missing gate"
        )
        result["details"]["blocking_reason"] = "tool_not_installed"
    elif proc.returncode != 0:
        violations = _parse_sa_output(proc.stdout, proc.stderr)
        result["violations"] = violations
        blocking = [
            v for v in violations if v.get("severity") in ("critical", "high")
        ]
        result["passed"] = len(blocking) == 0
        result["status"] = "fail" if blocking else "pass"
        result["details"]["message"] = (
            f"{len(violations)} issues found ({len(blocking)} critical/high)"
        )
    else:
        result["status"] = "pass"
        result["details"]["message"] = "Static analysis passed"

    return result


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def run_coverage_check(
    project_path: Path,
    feature_id: str,
    *,
    targets: list[QualityTarget] | None = None,
    subject_path: Path | None = None,
) -> dict:
    """Check coverage for every package affected by the feature."""
    subject_path = Path(subject_path or project_path).resolve()
    targets = targets or resolve_quality_targets(project_path, subject_path, feature_id)
    return _combine_target_results(
        "coverage",
        feature_id,
        subject_path,
        targets,
        lambda root: _run_coverage_for_root(project_path, feature_id, root),
    )


def _run_coverage_for_root(
    project_path: Path, feature_id: str, execution_path: Path
) -> dict:
    """Run coverage for one resolved language package.

    Producer contract: see run_linting docstring. details.stdout and
    details.stderr are strings.
    """
    result = {
        "check": "coverage",
        "feature_id": feature_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "passed": True,
        "details": {},
    }

    cov_cmd = _adapter_cmd_to_shell(execution_path, "coverage_command")
    if not cov_cmd:
        result["details"]["message"] = "No coverage tool configured"
        result["details"]["skip_reason"] = "no_coverage_tool"
        if _coverage_required(project_path, feature_id):
            result["status"] = "no_signal"
            result["passed"] = False
            _record_quality_audit(
                project_path,
                gate="quality",
                check="coverage",
                feature_id=feature_id,
                status="no_signal",
                reason="no_coverage_tool",
                mode="required",
            )
        else:
            # Adapter None means this stack has no declared coverage contract.
            # It is not the same as a declared command whose tool is missing;
            # that latter case still fails closed below.
            result["status"] = "not_applicable"
        return result

    result["details"]["command"] = cov_cmd

    proc = _run_quality_command(
        result, cov_cmd, execution_path,
        timeout=120, timeout_message="Coverage check timed out", error_label="Coverage",
    )
    if proc is None:
        return result
    if _tool_missing(proc.returncode, proc.stderr):
        result["passed"] = False
        result["status"] = "no_signal"
        result["details"]["message"] = (
            "required tool not installed (coverage) — install it or "
            "configure the stack; not skipping a missing gate"
        )
        result["details"]["blocking_reason"] = "tool_not_installed"
        return result

    coverage_pct = _extract_coverage_percentage(proc.stdout)
    result["details"]["coverage_percent"] = coverage_pct
    threshold = _get_coverage_threshold(project_path, feature_id)
    result["details"]["threshold"] = threshold

    if coverage_pct is not None:
        result["passed"] = coverage_pct >= threshold
        result["status"] = "pass" if coverage_pct >= threshold else "fail"
        result["details"]["message"] = (
            f"Coverage: {coverage_pct}% (threshold: {threshold}%)"
        )
    else:
        result["details"]["message"] = (
            "coverage output not parseable — blocking (no coverage signal)"
        )
        result["passed"] = False
        result["status"] = "no_signal"
        result["details"]["blocking_reason"] = "coverage_unparseable"

    return result


# ---------------------------------------------------------------------------
# Type check
# ---------------------------------------------------------------------------


def run_type_check(
    project_path: Path,
    feature_id: str,
    *,
    targets: list[QualityTarget] | None = None,
    subject_path: Path | None = None,
) -> dict:
    """Run configured type checks for the affected package roots."""
    subject_path = Path(subject_path or project_path).resolve()
    targets = targets or resolve_quality_targets(project_path, subject_path, feature_id)
    return _combine_target_results(
        "type_check",
        feature_id,
        subject_path,
        targets,
        lambda root: _run_type_check_for_root(project_path, feature_id, root),
    )


def _run_type_check_for_root(
    project_path: Path, feature_id: str, execution_path: Path
) -> dict:
    """Run the static type-check for one resolved language package.

    Producer contract: see run_linting docstring. details.stdout and
    details.stderr are strings.

    The adapter returns None when no type config is present (untyped
    Python, vanilla JS, gradually-typed TS) — that is a clean SKIP, not
    a block. A config-present-but-binary-absent case blocks like every
    other gate.
    """
    result = {
        "check": "type_check",
        "feature_id": feature_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "passed": True,
        "details": {},
    }

    tc_cmd = _adapter_cmd_to_shell(execution_path, "type_check_command")
    if not tc_cmd:
        result["details"]["message"] = "No type config detected"
        result["details"]["skip_reason"] = "no_type_config"
        result["status"] = "not_applicable"
        return result

    result["details"]["command"] = tc_cmd

    proc = _run_quality_command(
        result, tc_cmd, execution_path,
        timeout=120, timeout_message="Type check timed out", error_label="Type check",
    )
    if proc is None:
        return result
    if _tool_missing(proc.returncode, proc.stderr):
        result["passed"] = False
        result["status"] = "no_signal"
        result["details"]["message"] = (
            "required tool not installed (type-check) — install it or "
            "configure the stack; not skipping a missing gate"
        )
        result["details"]["blocking_reason"] = "tool_not_installed"
    elif proc.returncode != 0:
        result["passed"] = False
        result["status"] = "fail"
        result["details"]["message"] = "Type errors found"
    else:
        result["status"] = "pass"
        result["details"]["message"] = "Type check passed"

    return result


# ---------------------------------------------------------------------------
# Deliberately disabled checks
# ---------------------------------------------------------------------------


# Checks the standards gate no longer enforces, with the reason recorded in the
# evidence. An explicit, attested skip beats deleting the keys: an operator
# reading a standards artifact can tell these were switched off by decision
# rather than quietly passing, and the registry-vs-enforcement gap stays
# visible instead of becoming a hole someone has to remember to refill.
DISABLED_CHECKS = {
    "coverage": "disabled_by_policy_standards_gate_relocation",
    "type_check": "disabled_by_policy_standards_gate_relocation",
}


def disabled_check_result(check: str, feature_id: str) -> dict:
    """Build a ``not_applicable`` result for a check that is switched off.

    ``not_applicable`` (never ``pass``) so the check cannot satisfy a gate it
    no longer evaluates — ``get_quality_summary`` and ``run-all`` both treat
    ``not_applicable`` as non-contributing rather than as a success.
    """
    reason = DISABLED_CHECKS[check]
    return {
        "check": check,
        "feature_id": feature_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "passed": True,
        "status": "not_applicable",
        "details": {
            "message": f"{check} is disabled for the standards gate ({reason})",
            "skip_reason": reason,
            "disabled": True,
        },
    }


# ---------------------------------------------------------------------------
# Project-scoped standards run
# ---------------------------------------------------------------------------


# Sentinel feature_id for the project-scoped run. The per-check result dicts
# carry a feature_id field by contract; the project run has no feature, and a
# reserved token is more honest than an empty string or a borrowed real ID.
PROJECT_SUBJECT_ID = "__project__"


def project_standards_artifact(aah_path: Path, wave: int) -> Path:
    """Path of the project-scoped standards evidence for ``wave``.

    Deliberately NOT keyed by feature: the per-feature key is what made a
    deferred run impossible, because ``resolve_feature_subject`` binds it to a
    ``.claude/worktrees/<FID>`` that ``promote_to_develop`` has already deleted
    for every earlier wave.
    """
    return aah_path / "build" / "quality-results" / f"wave-{wave}-project-standards.json"


def build_standards_artifact(aah_path: Path) -> Path:
    """Path of the standards evidence for the lean, wave-free build.

    One artifact per build, named like ``regression-latest.json`` — the other
    once-per-build gate. Deliberately separate from
    ``project_standards_artifact``, which is the wave-keyed legacy path the
    orchestrator/verify wave code still reads; the lean build never reaches
    those, so nothing is shared and nothing there changes.
    """
    return aah_path / "build" / "quality-results" / "standards-latest.json"


def _recorded_commands(check_result: dict) -> str | list[str] | None:
    """The command(s) a check actually ran.

    ``_combine_target_results`` records ``details.command`` (a string) for a
    single target but ``details.commands`` (a list) once it folds several, so
    reading only the singular key recorded ``null`` for every multi-package run.
    """
    details = check_result.get("details", {})
    return details.get("commands") or details.get("command")


def run_project_standards(project_path: Path, wave: int | None = None) -> dict:
    """Run lint + static analysis ONCE over the whole codebase.

    Scope is the project root, not a per-feature worktree, and it fans out over
    EVERY package root found there. Resolving one adapter for the project was
    the defect this closes: ``detect()`` answers by fixed precedence with Python
    first, and Python's fingerprint glob reaches one level down, so a
    ``backend/pyproject.toml`` + ``frontend/package.json`` project ran
    ``ruff check backend`` and eslint never ran at all — a PASS that had never
    looked at the frontend.

    Coverage and type-check are recorded as disabled skips (see
    ``DISABLED_CHECKS``); neither runs here.
    """
    from aah.core.build.lang_checks import detect, discover_package_roots

    subject_path = project_path.resolve()
    targets = [
        QualityTarget(root, stack, ())
        for root, stack in discover_package_roots(subject_path)
    ]
    if not targets:
        # No package manifest anywhere. Keep the single-root shape so the
        # adapters report "no linter configured" (not_applicable) rather than
        # the gate silently examining nothing.
        targets = [QualityTarget(subject_path, detect(subject_path).name, ())]

    linting = run_linting(
        project_path, PROJECT_SUBJECT_ID,
        targets=targets, subject_path=subject_path,
    )
    sa = run_static_analysis(
        project_path, PROJECT_SUBJECT_ID,
        targets=targets, subject_path=subject_path,
    )
    cov = disabled_check_result("coverage", PROJECT_SUBJECT_ID)
    tc = disabled_check_result("type_check", PROJECT_SUBJECT_ID)

    overall = bool(
        linting["passed"] and sa["passed"] and cov["passed"] and tc["passed"]
    )
    check_statuses = {
        str(result.get("status") or "no_signal")
        for result in (linting, sa, cov, tc)
    }
    status = (
        "pass" if overall else
        "no_signal" if "no_signal" in check_statuses else
        "fail"
    )

    return {
        "scope": "project",
        "wave": wave,
        # Which packages were actually examined. Without this a PASS cannot be
        # told apart from "the frontend was never looked at" — the exact bug
        # the per-package fan-out fixes, so the evidence has to record it.
        "scopes": [_scope_metadata(target, subject_path) for target in targets],
        "overall_passed": overall,
        "verdict": "PASS" if overall else "BLOCK",
        "status": status,
        "linting": {
            "passed": linting["passed"],
            "status": linting.get("status"),
            "message": linting.get("details", {}).get("message", ""),
        },
        "static_analysis": {
            "passed": sa["passed"],
            "status": sa.get("status"),
            "message": sa.get("details", {}).get("message", ""),
        },
        "coverage": {"passed": cov["passed"], "status": cov["status"]},
        "type_check": {"passed": tc["passed"], "status": tc["status"]},
        # Verbatim tool output, which is the repair input. No violations array
        # and no severity mapping: the consumer is aah-fix, and it needs to read
        # what a developer would read.
        "findings": {
            "linting": linting.get("details", {}).get("stdout", ""),
            "static_analysis": sa.get("details", {}).get("stdout", ""),
        },
        "commands": {
            "linting": _recorded_commands(linting),
            "static_analysis": _recorded_commands(sa),
        },
        "_checks": {"linting": linting, "static_analysis": sa},
    }


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def get_quality_summary(project_path: Path, feature_id: str) -> dict:
    """Get a summary of all quality checks for a feature."""
    aah_path = project_path / ".aah"
    quality_dir = aah_path / "build" / "quality-results"

    summary = {
        "feature_id": feature_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "overall_passed": True,
        "checks": {},
        "blocking_issues": [],
    }

    check_types = ["linting", "static-analysis", "coverage", "type-check"]
    for check_type in check_types:
        result_path = quality_dir / f"{feature_id}-{check_type}.json"
        if result_path.exists():
            try:
                data = read_json(result_path)

                # remove optimistic defaults — derive status fail-closed
                status = data.get("status")
                if status is None:
                    # Legacy artifact with no status field: derive fail-closed
                    # (pass only if passed is explicitly True)
                    status = "pass" if data.get("passed") is True else "no_signal"

                summary["checks"][check_type] = {
                    "status": status,
                    "passed": data.get("passed"),
                    "message": data.get("details", {}).get("message", ""),
                }

                # gate satisfied only by "pass" or "not_applicable"
                # no_signal and fail both drop overall_passed
                if status not in ("pass", "not_applicable"):
                    summary["overall_passed"] = False

                # Collect blocking violations
                for v in data.get("violations", []):
                    if v.get("severity") in ("critical", "high"):
                        summary["blocking_issues"].append({
                            "check": check_type,
                            "severity": v.get("severity"),
                            "message": v.get("message", ""),
                        })
            except Exception:
                continue

    return summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _adapter_cmd_to_shell(project_path: Path, method_name: str) -> str | None:
    """Resolve a quality command via the language adapter pack.

    Returns a shell-string form so call sites preserve ``shell=True``
    semantics and the string stored in ``result.details.command``.

    The historical ` 2>/dev/null || true` fault-tolerance suffix was
    REMOVED. It made a
    missing tool exit 0, so an absent linter/scanner read as a *pass* —
    the silent-pass bug this effort closes. Callers now inspect the real
    exit code + stderr and distinguish "tool absent" (block) from "tool
    ran, found nothing / found violations" via _tool_missing().
    """
    import shlex
    from aah.core.build.lang_checks import detect

    adapter = detect(project_path)
    cmd = getattr(adapter, method_name)()
    if cmd is None:
        return None
    return shlex.join(cmd.argv)


# Stderr fragments that signal the *applicable* tool is not installed,
# rather than the tool having run and found problems. Commands run via
# wrappers (`uv run ruff`, `npx eslint`, `npm run lint`) so an absent
# inner tool does NOT surface as shell exit 127 — the wrapper is found and
# fails with its own code (1/2) plus one of these markers on stderr.
_TOOL_MISSING_MARKERS = (
    "was not found",              # uv: "The executable `ruff` was not found"
    "failed to spawn",            # uv: "Failed to spawn: `ruff`"
    "command not found",          # bash: "ruff: command not found"
    "not found",                  # sh/dash: "ruff: not found"
    "no such file",               # execvp ENOENT
    "could not determine executable",  # npx: no local/remote match
    "missing script",             # npm run <script> not defined
    "not recognized",             # windows "is not recognized as..."
)
# ponytail: "not found" is broad — a tool could theoretically print it in a
# real diagnostic. Fail-closed makes that a block (safe direction). Upgrade
# path: adapter tool_available() probe if a real tool trips this.


def _tool_missing(returncode: int, stderr: str) -> bool:
    """True iff a non-zero exit is because the tool is absent, not because
    it ran and found violations.

    ponytail: stderr-marker heuristic. The uv/npx/npm wrappers hide the
    would-be exit 127, so we key on their "not found" phrasing. Upgrade
    path: add an adapter-level tool_available() probe if false-blocks show
    up in practice.
    """
    if returncode == 0:
        return False
    s = (stderr or "").lower()
    return any(m in s for m in _TOOL_MISSING_MARKERS)


def _log_polyglot_limitation(project_path: Path, gated_stack: str) -> None:
    """Emit one stderr line if a second stack fingerprint is present.

    detect() gates exactly ONE stack; a genuinely polyglot repo (both a
    Python and a Node fingerprint, say) is only partially covered. We do
    not gate the second stack here (descoped) but we surface the gap so a
    green gate is not mistaken for full coverage.
    """
    from aah.core.build.lang_checks.node import (
        _FINGERPRINT_FILES as _NODE_FP,
    )
    from aah.core.build.lang_checks.python import (
        _FINGERPRINT_FILES as _PY_FP,
    )

    has_python = any((project_path / f).exists() for f in _PY_FP)
    has_node = any((project_path / f).exists() for f in _NODE_FP)
    if has_python and has_node:
        print(
            f"⚠ quality_checks: polyglot repo detected (Python + Node); "
            f"quality gate covers only the '{gated_stack}' stack. The other "
            f"stack's tools are NOT gated.",
            file=sys.stderr,
        )


def _resolve_linter(project_path: Path) -> str | None:
    """Detect appropriate linter command via the lang_checks adapter pack."""
    return _adapter_cmd_to_shell(project_path, "lint_command")


def _parse_sa_output(stdout: str, stderr: str) -> list[dict]:
    """Best-effort parsing of static analysis output."""
    violations = []
    output = stdout or stderr or ""

    # Try JSON parse first
    try:
        data = json.loads(output)
        if isinstance(data, dict) and "results" in data:
            for r in data["results"][:50]:
                violations.append({
                    "message": r.get("issue_text", r.get("message", "")),
                    "severity": r.get("issue_severity", "medium").lower(),
                    "source": "static_analysis",
                    "file": r.get("filename", ""),
                    "line": r.get("line_number"),
                })
            return violations
    except (json.JSONDecodeError, TypeError):
        pass

    # Fallback: line-based parsing.
    # Classify severities DISTINCTLY. The old code labelled any
    # line containing "high"/"severe"/"critical" as `critical`, which both
    # promoted real `high` findings to `critical` and made "high" unusable
    # as its own blocking tier. Test critical/severe first, then high, then
    # the lower tiers, so a line is mapped to its true severity.
    for line in output.split("\n"):
        line = line.strip()
        low = line.lower()
        if not ("severity" in low or "issue" in low or "vulnerability" in low):
            continue
        if any(k in low for k in ("critical", "severe")):
            severity = "critical"
        elif "high" in low:
            severity = "high"
        elif "medium" in low or "moderate" in low or "warning" in low:
            severity = "medium"
        elif "low" in low:
            severity = "low"
        else:
            severity = "medium"
        violations.append({
            "message": line[:200],
            "severity": severity,
            "source": "static_analysis",
        })
    return violations[:50]


def _extract_coverage_percentage(output: str) -> float | None:
    """Extract coverage percentage from tool output.

    Covers the shapes real projects emit so that blocking on
    unparseable output does not false-block non-pytest stacks:
      - pytest term:      "TOTAL   123   45   85%"
      - go test -cover:   "coverage: 85.3% of statements" (generic `coverage:`)
      - istanbul table    "All files | 85.7 | ..." — jest / vitest / c8 all
                          render the same istanbul summary; the "All files"
                          row's first percentage column is % Stmts.
      - jest text summary "Statements   : 85.7% ( ... )"
      - generic           "Coverage: 85.3%" / trailing "85%".
    """
    import re
    patterns = [
        r"TOTAL\s+\d+\s+\d+\s+(\d+)%",
        # istanbul table (jest/vitest/c8): "All files | 85.7 | 72 | ..."
        r"All files\s*\|\s*(\d+(?:\.\d+)?)",
        r"Statements\s*:\s*(\d+(?:\.\d+)?)%",
        r"coverage[:\s]+(\d+(?:\.\d+)?)%",
        r"(\d+(?:\.\d+)?)%\s*$",
    ]
    for pattern in patterns:
        match = re.search(pattern, output, re.MULTILINE | re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def _get_coverage_threshold(project_path: Path, feature_id: str | None = None) -> float:
    """Get the coverage threshold for a feature.

    Honors the per-feature coverage ratchet written by
    determine_checkpoints into
    standards.per_feature_coverage_floors[feature_id]. When a raised floor is
    set for this feature it wins; otherwise fall back to the global
    standards.coverage_threshold, then the 60.0 default. Without this
    per-feature read-back the ratchet would be an inert artifact.
    """
    config_path = project_path / ".aah" / "plan" / "checkpoint-config.yaml"
    if config_path.exists():
        try:
            config = read_yaml(config_path)
            standards = config.get("checkpoint_configuration", {}).get("standards", {})
            if feature_id is not None:
                floor = standards.get("per_feature_coverage_floors", {}).get(feature_id)
                if floor is not None:
                    return float(floor)
            threshold = standards.get("coverage_threshold")
            if threshold is not None:
                return float(threshold)
        except Exception:
            pass
    return 60.0  # Default threshold


def _coverage_required(project_path: Path, feature_id: str) -> bool:
    """Whether planning explicitly requires coverage for this feature.

    A generated checkpoint config is authoritative: a per-feature floor or a
    global threshold selects coverage. A manifest without checkpoint config is
    an incomplete planned project and fails closed. Bare adapter/library use
    has no AAH coverage contract, so an adapter returning None is N/A.
    """
    config_path = project_path / ".aah" / "plan" / "checkpoint-config.yaml"
    if config_path.exists():
        try:
            config = read_yaml(config_path) or {}
            standards = (
                config.get("checkpoint_configuration", {}).get("standards", {})
            )
            floors = standards.get("per_feature_coverage_floors", {}) or {}
            return (
                feature_id in floors
                or standards.get("coverage_threshold") is not None
            )
        except Exception:
            return True
    return (project_path / ".aah" / "manifest.yaml").exists()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_project_command(project_path: Path, aah_path: Path, args) -> None:
    """Execute and persist the project-scoped standards run, then exit.

    The subject is the project root on the build branch (or, on the legacy wave
    path, the wave's integration branch) — the one whole-codebase subject that
    still exists at the end of a project. It follows the same seam
    ``resolve_feature_subject`` falls back to when no worktree is present, so the
    identity contract is unchanged; only the scope is.

    ``--wave`` is what selects the artifact path: absent (the lean build) writes
    ``standards-latest.json``; present keeps the wave-keyed legacy name.

    The run is READ-ONLY on the subject: ``capture_subject`` → checks →
    ``assert_subject_unchanged``. It never installs anything — that is the build
    skill's pre-gate backstop's job, so this property stays unbreakable.
    """
    started = time.monotonic()
    subject_path = project_path.resolve()
    subject_before = capture_subject(subject_path, project_path)
    if args.subject_branch and subject_before.get("branch") != args.subject_branch:
        raise EvidenceError(
            f"Subject branch mismatch: expected {args.subject_branch}, "
            f"got {subject_before.get('branch')}"
        )
    if args.subject_sha and subject_before.get("commit_sha") != args.subject_sha:
        raise EvidenceError(
            f"Subject SHA mismatch: expected {args.subject_sha}, "
            f"got {subject_before.get('commit_sha')}"
        )
    if subject_before.get("clean") is not True:
        raise EvidenceError("Standards subject contains non-framework changes")

    summary = run_project_standards(project_path, args.wave)
    checks = summary.pop("_checks")

    assert_subject_unchanged(subject_before, subject_path, project_path)
    summary["subject"] = capture_subject(subject_path, project_path)
    summary["schema_version"] = 1

    cumulative_stdout = "".join(
        f"=== {name} ===\n{checks[name].get('details', {}).get('stdout', '')}\n"
        for name in ("linting", "static_analysis")
    )
    cumulative_stderr = "".join(
        f"=== {name} ===\n{checks[name].get('details', {}).get('stderr', '')}\n"
        for name in ("linting", "static_analysis")
    )
    overall = bool(summary["overall_passed"])

    if args.wave is None:
        artifact_path = build_standards_artifact(aah_path)
        artifact_name = "project standards check"
    else:
        artifact_path = project_standards_artifact(aah_path, args.wave)
        artifact_name = f"wave-{args.wave} project standards check"

    write_attested(
        summary,
        artifact_path,
        project_path=project_path,
        command=COMMAND_PREFIX + sys.argv[1:],
        exit_code=0 if overall else 1,
        stdout=cumulative_stdout,
        stderr=cumulative_stderr,
        duration_ms=int((time.monotonic() - started) * 1000),
        artifact_name=artifact_name,
    )

    json.dump(summary, sys.stdout, indent=2)
    print()
    sys.exit(0 if overall else 2)


def main() -> None:
    parser = argparse.ArgumentParser(description="AAH Quality Checks")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common_args(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--project-path", type=Path, default=None)
        command_parser.add_argument("--feature-id", type=str, required=True)
        command_parser.add_argument("--subject-path", type=Path, default=None)
        command_parser.add_argument("--subject-branch", type=str, default=None)
        command_parser.add_argument("--subject-sha", type=str, default=None)

    lint_p = sub.add_parser("linting", help="Run linting checks")
    add_common_args(lint_p)

    sa_p = sub.add_parser("static-analysis", help="Run static analysis")
    add_common_args(sa_p)

    cov_p = sub.add_parser("coverage", help="Run coverage check")
    add_common_args(cov_p)

    tc_p = sub.add_parser("type-check", help="Run static type check")
    add_common_args(tc_p)

    sum_p = sub.add_parser("summary", help="Get quality check summary")
    add_common_args(sum_p)

    run_all_p = sub.add_parser("run-all", help="Run all quality checks")
    add_common_args(run_all_p)

    run_project_p = sub.add_parser(
        "run-project",
        help="Run whole-codebase standards checks over every package root",
    )
    run_project_p.add_argument("--project-path", type=Path, default=None)
    run_project_p.add_argument(
        "--wave", type=int, default=None,
        help="Legacy wave path. Omit for the lean build (writes standards-latest.json)",
    )
    run_project_p.add_argument("--subject-branch", type=str, default=None)
    run_project_p.add_argument("--subject-sha", type=str, default=None)

    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(
        getattr(args, "project_path", None)
    )
    aah_path = project_path / ".aah"

    # Ensure output directory
    quality_dir = aah_path / "build" / "quality-results"
    quality_dir.mkdir(parents=True, exist_ok=True)

    if args.command == "summary":
        result = get_quality_summary(project_path, args.feature_id)
        json.dump(result, sys.stdout, indent=2)
        print()
        sys.exit(0 if result["overall_passed"] else 2)

    if args.command == "run-project":
        _run_project_command(project_path, aah_path, args)
        return

    subject_path = Path(args.subject_path or project_path).resolve()
    subject_before = capture_subject(subject_path, project_path)
    if args.subject_branch and subject_before.get("branch") != args.subject_branch:
        raise EvidenceError(
            f"Subject branch mismatch: expected {args.subject_branch}, "
            f"got {subject_before.get('branch')}"
        )
    if args.subject_sha and subject_before.get("commit_sha") != args.subject_sha:
        raise EvidenceError(
            f"Subject SHA mismatch: expected {args.subject_sha}, "
            f"got {subject_before.get('commit_sha')}"
        )
    if subject_before.get("clean") is not True:
        raise EvidenceError("Standards subject contains non-framework changes")
    feature_path = find_feature_file(
        subject_path / ".aah" / "plan" / "features", args.feature_id
    )
    if feature_path is None:
        raise EvidenceError(f"Feature contract not found: {args.feature_id}")
    targets = resolve_quality_targets(project_path, subject_path, args.feature_id)

    if args.command == "linting":
        result = run_linting(
            project_path, args.feature_id, targets=targets, subject_path=subject_path
        )
        assert_subject_unchanged(subject_before, subject_path, project_path)
        result["subject"] = capture_subject(subject_path, project_path)
        write_json(result, quality_dir / f"{args.feature_id}-linting.json")
        json.dump(result, sys.stdout, indent=2)
        print()
        sys.exit(0 if result["passed"] else 2)

    elif args.command == "static-analysis":
        result = run_static_analysis(
            project_path, args.feature_id, targets=targets, subject_path=subject_path
        )
        assert_subject_unchanged(subject_before, subject_path, project_path)
        result["subject"] = capture_subject(subject_path, project_path)
        write_json(result, quality_dir / f"{args.feature_id}-static-analysis.json")
        json.dump(result, sys.stdout, indent=2)
        print()
        sys.exit(0 if result["passed"] else 2)

    elif args.command == "coverage":
        result = run_coverage_check(
            project_path, args.feature_id, targets=targets, subject_path=subject_path
        )
        assert_subject_unchanged(subject_before, subject_path, project_path)
        result["subject"] = capture_subject(subject_path, project_path)
        write_json(result, quality_dir / f"{args.feature_id}-coverage.json")
        json.dump(result, sys.stdout, indent=2)
        print()
        sys.exit(0 if result["passed"] else 2)

    elif args.command == "type-check":
        result = run_type_check(
            project_path, args.feature_id, targets=targets, subject_path=subject_path
        )
        assert_subject_unchanged(subject_before, subject_path, project_path)
        result["subject"] = capture_subject(subject_path, project_path)
        write_json(result, quality_dir / f"{args.feature_id}-type-check.json")
        json.dump(result, sys.stdout, indent=2)
        print()
        sys.exit(0 if result["passed"] else 2)

    elif args.command == "run-all":
        # Run all checks sequentially. Capture cumulative subprocess
        # metadata across the three sub-runs for the attestation block
        # on the consolidated standards file.
        started = time.monotonic()

        linting = run_linting(
            project_path, args.feature_id, targets=targets, subject_path=subject_path
        )
        write_json(linting, quality_dir / f"{args.feature_id}-linting.json")

        sa = run_static_analysis(
            project_path, args.feature_id, targets=targets, subject_path=subject_path
        )
        write_json(sa, quality_dir / f"{args.feature_id}-static-analysis.json")

        # Coverage and type-check are DISABLED for this gate. They are recorded
        # as an explicit not_applicable skip rather than dropped, so the
        # evidence says "switched off by decision" instead of leaving an
        # operator to infer it from two absent keys. Neither runs, so neither
        # invokes pytest — which is why this gate needs no tests path and no
        # resolved project dependencies.
        for check, path_suffix in (
            ("coverage", "coverage"), ("type_check", "type-check"),
        ):
            _record_quality_audit(
                project_path,
                gate="quality",
                check=check,
                feature_id=args.feature_id,
                status="not_applicable",
                reason=DISABLED_CHECKS[check],
                mode="disabled",
            )
            write_json(
                disabled_check_result(check, args.feature_id),
                quality_dir / f"{args.feature_id}-{path_suffix}.json",
            )
        cov = disabled_check_result("coverage", args.feature_id)
        tc = disabled_check_result("type_check", args.feature_id)

        overall = (
            linting["passed"] and sa["passed"]
            and cov["passed"] and tc["passed"]
        )
        # Determine verdict for orchestrator artifact gate compatibility
        verdict = "PASS" if overall else "BLOCK"
        check_statuses = {
            str(result.get("status") or "no_signal")
            for result in (linting, sa, cov, tc)
        }
        evidence_status = (
            "pass" if overall else
            "no_signal" if "no_signal" in check_statuses else
            "fail"
        )
        assert_subject_unchanged(subject_before, subject_path, project_path)
        subject_after = capture_subject(subject_path, project_path)

        summary = {
            "feature_id": args.feature_id,
            "overall_passed": overall,
            "verdict": verdict,
            "linting": {"passed": linting["passed"]},
            "static_analysis": {"passed": sa["passed"]},
            "coverage": {"passed": cov["passed"]},
            "type_check": {"passed": tc["passed"]},
            "quality_scopes": [
                _scope_metadata(target, subject_path) for target in targets
            ],
        }
        summary.update(build_evidence_v2_record(
            feature_id=args.feature_id,
            producer="quality_checks",
            subject=subject_after,
            contract_hash=hash_feature_contract(feature_path),
            test_input_hash=hash_test_inputs([], subject_path),
            test_paths=[],
            execution={
                "actor": "system",
                "attempt_id": "standards",
                "argv": list(sys.argv[1:]),
                "cwd": subject_after.get("rel_path"),
            },
            status=evidence_status,
            artifacts={"quality_scopes": summary["quality_scopes"]},
        ))

        # Compose attestation metadata: concatenate sub-check outputs
        # so the signature binds to what each sub-run actually produced.
        # Falls back to empty strings when a sub-check skipped (no linter/
        # tool available) since those code paths populate `details`
        # without exit_code/stdout/stderr.
        #
        # Producers (run_linting/run_static_analysis/
        # run_coverage_check) MUST place strings in details.stdout and
        # details.stderr — see their docstrings for the contract. A
        # non-string indicates a producer-side regression; warn loudly
        # then coerce so the attestation still composes (it would be
        # worse to crash the writer over a logging-level concern).
        def _detail(d: dict, key: str) -> str:
            val = d.get("details", {}).get(key, "")
            if not isinstance(val, str):
                print(
                    f"⚠ quality_checks._detail: details.{key} is not str "
                    f"(got {type(val).__name__}); coercing. Fix the producer.",
                    file=sys.stderr,
                )
                return str(val)
            return val
        cumulative_stdout = (
            f"=== linting ===\n{_detail(linting, 'stdout')}\n"
            f"=== static-analysis ===\n{_detail(sa, 'stdout')}\n"
            f"=== coverage ===\n{_detail(cov, 'stdout')}\n"
            f"=== type-check ===\n{_detail(tc, 'stdout')}\n"
        )
        cumulative_stderr = (
            f"=== linting ===\n{_detail(linting, 'stderr')}\n"
            f"=== static-analysis ===\n{_detail(sa, 'stderr')}\n"
            f"=== coverage ===\n{_detail(cov, 'stderr')}\n"
            f"=== type-check ===\n{_detail(tc, 'stderr')}\n"
        )
        # Aggregate exit code: 0 if all passed, else 1.
        exit_code = 0 if overall else 1
        duration_ms = int((time.monotonic() - started) * 1000)

        # Write the combined standards file (artifact gate depends on this exact path)
        write_attested(
            summary,
            quality_dir / f"{args.feature_id}-standards.json",
            project_path=project_path,
            command=COMMAND_PREFIX + sys.argv[1:],
            exit_code=exit_code,
            stdout=cumulative_stdout,
            stderr=cumulative_stderr,
            duration_ms=duration_ms,
            artifact_name=f"{args.feature_id} standards check",
        )

        json.dump(summary, sys.stdout, indent=2)
        print()
        sys.exit(0 if overall else 2)


if __name__ == "__main__":
    main()
