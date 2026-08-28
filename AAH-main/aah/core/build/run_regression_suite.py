#!/usr/bin/env python3
"""
Run cumulative regression test suite across all completed features.

Executes all tests for features marked as passing in feature-list.json.

Result contract (consumed by the orchestrator and git_ops gating logic)
-----------------------------------------------------------------------
Every result dict carries a ``status`` field which is the source of truth
for gating. It takes exactly one of three values:

- ``"pass"``      — the suite ran and every test passed. ``passed`` is True.
                    Exit 0. A merge gate MAY be satisfied by this.
- ``"fail"``      — the suite ran (or timed out / errored) and there were
                    failures. ``passed`` is False. Exit 2. A merge gate MUST
                    block on this.
- ``"no_signal"`` — there were NO passing features to regress (empty
                    feature-list, missing feature-list, or no passing
                    test-results). The suite did not run, so there is NO
                    regression signal. ``passed`` is False (it is NOT a
                    satisfied gate — a vacuously-green result must never
                    gate a merge). Exit 0.

IMPORTANT for gating consumers: ``no_signal`` is NEITHER pass NOR fail.
- Do NOT treat it as a satisfied gate (do not merge purely on the basis
  of a no_signal regression result).
- Do NOT treat it as a hard failure either — a genuine early wave-0 with
  no prior passing features legitimately produces no_signal and must not
  hard-fail. Consumers should branch on ``status == "no_signal"`` rather
  than reading ``passed`` alone.

Exit codes: 0 = pass OR no_signal, 2 = fail.
"""

import argparse
import json
import shlex
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from aah.core.build.evidence import (
    EvidenceError,
    make_run_id,
    read_evidence_retention,
    write_attested_result,
    write_failure_bundle,
)
from aah.core.build.run_feature_tests import (
    _parse_junit_xml,
    _redirected_test_environment,
    _safe_command_argv,
    _sanitize_test_output,
    _secret_patterns,
    discover_test_reports,
    resolve_feature_set_required_env,
)
from aah.core.common.execution import CommandSpec, run_bounded_command
from aah.core.common.feature_list import get_passing_features, load_feature_list
from aah.core.common.git_utils import (
    AAH_STATE_PATHS,
    code_subject_identity,
    current_branch,
    porcelain_dirt,
)
from aah.core.common.io_utils import read_json

# Canonical command prefix recorded in the attestation block. The
# verify.read_regression_evidence() uses this prefix in
# expected_command_prefix to confirm the file came from this writer.
COMMAND_PREFIX = ["aah", "run", "core.build.run_regression_suite"]


def _tracked_dirt(project_path: Path, prefixes=AAH_STATE_PATHS) -> list[str]:
    """Tracked modifications outside `prefixes` — see git_utils.porcelain_dirt."""
    return porcelain_dirt(project_path, prefixes, tracked_only=True)


def _regression_required_env(
    project_path: Path,
    passing_features: list[dict],
) -> tuple[dict[str, str], list[str]]:
    """Resolve the declared env-key union without exposing unrelated secrets."""
    feature_ids = [
        feature["id"] for feature in passing_features
        if isinstance(feature, dict) and isinstance(feature.get("id"), str)
    ]
    return resolve_feature_set_required_env(project_path, feature_ids)


def run_regression_suite(
    project_path: Path,
    *,
    wave: int | None = None,
    subject_branch: str | None = None,
) -> dict:
    """Run cumulative regression tests and return structured results.

    Ensures required infrastructure is running before executing tests.

    Args:
        project_path: Root of the project
        subject_branch: v2 (lean/sequential) — if provided, assert the current
            branch equals it (the single build branch, e.g. ``build/<project>``).
            This is the wave-free branch proof.
        wave: Legacy — if provided (and ``subject_branch`` is not), assert the
            current branch is ``integration/wave-{wave}``.
    """
    from aah.core.build.ensure_infra import cleanup_namespace, ensure_test_environment

    aah_path = project_path / ".aah"

    # Track elapsed time for attestation regardless of which return path fires.
    started = time.monotonic()

    # --- Preconditions, BEFORE any environment setup or test execution ---
    # Capture the .aah/.claude-excluding content identity first. Producers and
    # gates always compare this same identity; raw HEAD is audit metadata only.
    try:
        branch = current_branch(cwd=project_path)
        head = code_subject_identity(cwd=project_path)
        if head is None:
            raise RuntimeError("code subject identity is unavailable")
    except Exception as e:
        return _no_op_result(
            started,
            f"Failed to read current branch/HEAD: {e}",
            branch=None,
            head=None,
        )

    # If wave is provided, validate we're on the expected integration branch.
    # A wrong-branch refusal is NOT no_signal: no_signal means "the suite
    # ran/attempted and found no passing features to regress" (a real,
    # non-fatal signal). Wrong-branch means the suite never even attempted
    # here — the caller must check out the right branch and re-run. Tag it
    # distinctly so gating consumers treat it as "re-run", never as a hard
    # regression failure or a satisfied gate.
    expected_branch = None
    if subject_branch is not None:
        expected_branch = subject_branch
    elif wave is not None:
        expected_branch = f"integration/wave-{wave}"
    if expected_branch is not None and branch != expected_branch:
        return _wrong_branch_result(
            started,
            branch=branch,
            expected_branch=expected_branch,
            head=head,
        )

    # Application changes must not be in flight — the suite would measure a
    # subject nobody attested. `.aah/`/`.claude/` churn is excluded, and so are
    # untracked files (see _tracked_dirt).
    dirt_before = _tracked_dirt(project_path)
    if dirt_before:
        refusal = _no_op_result(
            started,
            (
                "Working tree has tracked changes outside "
                f"{', '.join(AAH_STATE_PATHS)} — regression not run. "
                "Commit or revert them and re-run."
            ),
            branch=branch,
            head=head,
        )
        refusal["signal_reason"] = "tree_dirty_before_regression"
        refusal["dirty"] = dirt_before
        return refusal

    # Regression always owns an isolated, collision-resistant run namespace.
    run_id = make_run_id(
        project=project_path.name,
        wave=wave,
        feature="regression",
        actor="regression",
        attempt="attempt",
    )

    # Full test environment setup: deps + Docker + services.
    infra = ensure_test_environment(project_path, run_id=run_id)
    docker_cleanup_needed = bool(infra.get("docker_available"))
    infra_warning = None

    def _cleanup() -> dict | None:
        if not docker_cleanup_needed:
            return {
                "transport": "none",
                "action": "cleanup_namespace",
                "namespace": run_id,
                "removed": [],
                "leftover": [],
                "errors": [],
                "stopped": True,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        try:
            return cleanup_namespace(run_id, project_path)
        except Exception:
            return None

    # A REQUIRED-service setup failure is a DISTINCT block (not a soft
    # warning). It short-circuits with a no_signal result.
    if infra.get("setup_status") == "no_signal":
        _cleanup()
        blocked = _no_op_result(
            started,
            f"Required test infrastructure unavailable: {infra.get('required_failures')}",
            branch=branch,
            head=head,
        )
        blocked["block"] = True
        blocked["required_failures"] = infra.get("required_failures", [])
        return blocked

    if not infra["ready"]:
        infra_warning = f"Test environment not ready: {infra['message']}. Tests will proceed anyway."

    def _subject_drifted() -> str | None:
        """Stable signal_reason if branch/subject/cleanliness moved, else None.

        Called after the command AND after isolated-env cleanup, so a cleanup
        that mutates the subject is caught too.
        """
        try:
            branch_after = current_branch(cwd=project_path)
        except Exception:
            return "branch_changed_during_regression"
        if branch_after != branch:
            return "branch_changed_during_regression"
        try:
            head_after = code_subject_identity(cwd=project_path)
        except Exception:
            return "subject_changed_during_regression"
        if head_after is None:
            return "subject_changed_during_regression"
        if head_after != head:
            return "subject_changed_during_regression"
        if _tracked_dirt(project_path):
            return "tree_dirty_after_regression"
        return None

    # Load feature list
    fl_path = aah_path / "feature-list.json"
    if not fl_path.exists():
        _cleanup()
        return _no_op_result(
            started,
            "No feature-list.json — nothing to regress",
            branch=branch,
            head=head,
        )

    fl_data = load_feature_list(fl_path)
    passing_features = get_passing_features(fl_data)

    # Supersede exclusion (forward-relocation model): a live rework entry
    # (e.g. F2-rework-01) that carries a `supersedes: F2` marker AND whose own
    # tests pass replaces the feature it supersedes. Drop the superseded ID
    # from the regression set so the old (now-obsolete) feature's expectations
    # don't gate the suite. Only excludes when the superseding entry exists and
    # its tests pass — an in-flight rework never drops coverage. This is the
    # ONLY reader that acts on the `supersedes` marker.
    exclusions = _supersede_exclusions(aah_path, fl_data)
    if exclusions:
        passing_features = [f for f in passing_features if f.get("id") not in exclusions]

    # Fallback: if feature-list shows no passing features, check test-results
    # directory for features with passing test results. This handles the case
    # where tests passed but feature-list wasn't updated yet.
    if not passing_features:
        test_results_dir = aah_path / "build" / "test-results"
        if test_results_dir.is_dir():
            for result_file in sorted(test_results_dir.glob("F*.json")):
                try:
                    result_data = read_json(result_file)
                    if result_data.get("passed"):
                        passing_features.append({"id": result_data.get("feature_id", result_file.stem)})
                except Exception:
                    pass

    if not passing_features:
        _cleanup()
        return _no_op_result(started, "No passing features to regress", branch=branch, head=head)

    try:
        resolved_required_env, missing_env = _regression_required_env(
            project_path, passing_features
        )
    except EvidenceError as exc:
        _cleanup()
        blocked = _no_op_result(
            started,
            f"Invalid required_env declaration: {exc}",
            branch=branch,
            head=head,
        )
        blocked.update({
            "block": True,
            "signal_reason": "invalid_required_env",
            "env_validation_errors": [str(exc)],
        })
        return blocked
    if missing_env:
        _cleanup()
        blocked = _no_op_result(
            started,
            "Missing required environment keys "
            f"{missing_env}: add values to the project root .env "
            "(see .env.example), then re-run.",
            branch=branch,
            head=head,
        )
        blocked.update({
            "block": True,
            "signal_reason": "missing_required_env",
            "missing_required_env": missing_env,
        })
        return blocked

    # Determine test command — create temp file for JUnit XML
    junit_tmpfile = tempfile.NamedTemporaryFile(suffix=".xml", delete=False, prefix="aah-regression-")
    junit_path = Path(junit_tmpfile.name)
    junit_tmpfile.close()

    # Dispatch to the language adapter pack instead of
    # inlining per-language branches. The adapter consults
    # manifest.stack_choices.primary AND the project's file
    # fingerprints to pick the right adapter, then returns a
    # structured Cmd. We unparse argv back to a shell string because the
    # runner preserves shell=True semantics for compatibility.
    from aah.core.build.lang_checks import resolve_test_command

    junit_flag = f" --junitxml={junit_path}"
    cmd = resolve_test_command(project_path)
    if cmd is None:
        # Generic adapter / no recognized stack: fall back to the
        # framework's historical default (pytest), preserving
        # behavior for projects with un-set stack_choices.primary
        # that still use pytest convention.
        test_cmd = f"uv run pytest tests/ -v --tb=short{junit_flag}"
    else:
        test_cmd = shlex.join(cmd.argv)
    if "pytest" in test_cmd and "--junitxml" not in test_cmd:
        test_cmd += junit_flag

    # Route pytest output/caches away from the subject via a per-run redirected
    # environment (adopts the run_feature_tests contract).
    run_namespace_dir = tempfile.TemporaryDirectory(prefix=f"{run_id}-")
    run_env = _redirected_test_environment(
        Path(run_namespace_dir.name),
        resolved_required_env,
        project_path=project_path,
    )

    def _write_bundle(logs: dict, exit_code: int, cleanup: dict | None) -> dict | None:
        """Write a sanitized failure bundle for a failing regression run."""
        try:
            retention = read_evidence_retention(project_path)
            subject = {"branch": branch, "commit_sha": head, "rel_path": "."}
            execution = {
                "argv": _safe_command_argv(test_cmd),
                "cwd": ".",
                "adapter": None,
                "seed": run_env.get("PYTHONHASHSEED"),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "exit_code": exit_code,
                "project_root": str(project_path),
            }
            bundle_dir = write_failure_bundle(
                run_id=run_id,
                subject=subject,
                execution=execution,
                cleanup=cleanup,
                diagnostic=None,
                logs=logs,
                retention=retention,
                out_dir=project_path / ".aah" / "build" / "failure-bundles",
            )
            return {"dir": str(bundle_dir), "manifest": "reproduction.json"}
        except EvidenceError:
            return None

    def _execution_failure(error: str, exit_code: int) -> dict:
        duration_ms = int((time.monotonic() - started) * 1000)
        failed = {
            "status": "fail",
            "passed": False,
            "error": _sanitize_test_output(error, resolved_required_env),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "subject": {"branch": branch, "commit_sha": head},
            "_attestation_meta": {
                "command": shlex.split(test_cmd),
                "exit_code": exit_code,
                "stdout": "",
                "stderr": _sanitize_test_output(error, resolved_required_env),
                "duration_ms": duration_ms,
            },
        }
        cleanup = _cleanup()
        if cleanup is not None:
            failed["cleanup"] = cleanup
        bundle = _write_bundle(
            {"stderr": _sanitize_test_output(error, resolved_required_env)},
            exit_code,
            cleanup,
        )
        if bundle:
            failed.setdefault("artifacts", {})["failure_bundle"] = bundle
        return failed

    # Run the full test suite
    try:
        result = run_bounded_command(
            CommandSpec(
                test_cmd,
                shell=True,
                cwd=project_path,
                env=run_env,
                timeout_sec=600,
            )
        )
        if result.state == "timeout":
            return _execution_failure("Regression suite timed out (600s)", 124)
        if result.state != "executed":
            return _execution_failure(result.error or result.state, result.returncode)
        if result.stdout_truncated or result.stderr_truncated:
            return _execution_failure("Regression suite output truncated", 126)

        duration_ms = int((time.monotonic() - started) * 1000)

        passed = result.returncode == 0

        # Parse JUnit XML for structured per-test results. Discovery covers
        # the runners that ignore --junitxml and write to their own
        # conventional location (Maven Surefire, Gradle, a JUnit-configured
        # Playwright/vitest/gotestsum/nextest), so the final gate is not
        # pytest-only.
        report_files = discover_test_reports(junit_path, project_path)
        junit_data = _parse_junit_xml(
            report_files,
            extra_patterns=tuple(_secret_patterns(resolved_required_env)),
        )
        safe_stdout = _sanitize_test_output(result.stdout, resolved_required_env)
        safe_stderr = _sanitize_test_output(result.stderr, resolved_required_env)

        regression_result = {
            "status": "pass" if passed else "fail",
            "passed": passed,
            "exit_code": result.returncode,
            "command": test_cmd,
            "total_features_tested": len(passing_features),
            "summary": junit_data["summary"],
            "test_cases": junit_data["test_cases"],
            "failures": junit_data["failures"],
            "stdout_tail": safe_stdout[-2000:],
            "stderr_tail": safe_stderr[-1000:],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "subject": {
                "branch": branch,
                "commit_sha": head,
            },
            # Private metadata for the attestation block; popped in main().
            # Carries the FULL stdout/stderr (not the truncated copies above)
            # so the attestation hash binds to what actually ran.
            "_attestation_meta": {
                "command": shlex.split(test_cmd),
                "exit_code": result.returncode,
                "stdout": safe_stdout,
                "stderr": safe_stderr,
                "duration_ms": duration_ms,
            },
        }

        # If JUnit XML was empty (e.g. non-pytest runner), fall back to text parsing
        if junit_data["summary"]["total"] == 0:
            regression_result["summary"]["total"] = _count_tests(safe_stdout)
            if not passed:
                for line in safe_stdout.split("\n"):
                    if line.strip().startswith("FAILED") or line.strip().startswith("FAIL:"):
                        regression_result["failures"].append({"name": line.strip()[:200], "classname": "", "message": "See test output", "traceback": ""})

        if infra_warning:
            regression_result["infra_warning"] = infra_warning

        cleanup = _cleanup()
        if cleanup is not None:
            regression_result["cleanup"] = cleanup
        if not passed:
            bundle = _write_bundle(
                {"stdout": safe_stdout, "stderr": safe_stderr},
                result.returncode,
                cleanup,
            )
            if bundle:
                regression_result.setdefault("artifacts", {})["failure_bundle"] = bundle

        # A nominal pass whose report cannot be read proves nothing either: an
        # all-zero summary is indistinguishable from "the suite is empty and
        # everything is fine", which is exactly how a pytest-only parse let a
        # green regression stand over never-executed Maven and Playwright
        # suites. Demote to no_signal so the final gate cannot pass blind. A
        # `fail` verdict stands — an unreadable report cannot rescue it.
        if passed and not report_files:
            regression_result["status"] = "no_signal"
            regression_result["passed"] = False
            regression_result["signal_reason"] = "test_report_unreadable"
            regression_result["message"] = (
                "Regression tests exited 0 but wrote no readable JUnit report, "
                "so the suite cannot be verified as having run. Configure the "
                "test runner to emit JUnit XML (pytest --junitxml is injected "
                "automatically; Maven/Gradle write one by default; Playwright, "
                "vitest, gotestsum and nextest need a JUnit reporter named in "
                "project config)."
            )
            return regression_result

        # A nominal pass over a subject that moved mid-run proves nothing. Demote
        # it to no_signal — never emit passed: true. A `fail` verdict stands: the
        # tests really did fail, and drift cannot make that a pass.
        if passed:
            drift = _subject_drifted()
            if drift:
                regression_result["status"] = "no_signal"
                regression_result["passed"] = False
                regression_result["signal_reason"] = drift
                regression_result["message"] = (
                    f"Regression tests passed but the subject changed during the "
                    f"run ({drift}) — the result proves nothing about the current "
                    "subject. Re-run on a stable integration branch."
                )
        return regression_result

    except Exception as exc:
        return _execution_failure(str(exc), 1)
    finally:
        junit_path.unlink(missing_ok=True)
        run_namespace_dir.cleanup()


def _normalize_supersedes(value) -> list[str]:
    """Coerce a `supersedes` field into a flat list of feature IDs.

    The marker may be parsed as a scalar (``"F2"``) or a single-item list
    (``["F2"]``) depending on how the feature .md was written. Normalize both.
    """
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [str(value)]


def _rework_entry_tests_pass(aah_path: Path, rework_id: str) -> bool:
    """True iff the rework entry's per-feature test result exists and passed."""
    result_path = aah_path / "build" / "test-results" / f"{rework_id}.json"
    if not result_path.exists():
        return False
    try:
        data = read_json(result_path)
    except Exception:
        return False
    return bool(isinstance(data, dict) and data.get("passed"))


def _supersede_exclusions(aah_path: Path, fl_data: dict) -> set[str]:
    """Return the set of feature IDs superseded by a passing rework entry.

    A feature ID is excluded only when a live rework entry names it via
    ``supersedes`` AND that rework entry's own tests pass. This keeps coverage
    intact while a rework is still in flight.
    """
    exclusions: set[str] = set()
    for feature in fl_data.get("features", []):
        superseded = _normalize_supersedes(feature.get("supersedes"))
        if not superseded:
            continue
        rework_id = feature.get("id")
        if rework_id and _rework_entry_tests_pass(aah_path, rework_id):
            exclusions.update(superseded)
    return exclusions


def _no_op_result(
    started: float,
    message: str,
    *,
    branch: str | None = None,
    head: str | None = None,
) -> dict:
    """Build an early-return result when there is no regression signal.

    The suite did not run, so the result is not eligible to satisfy a merge
    gate. It still exits cleanly for legitimate early waves with no prior
    passing features.

    Includes the same _attestation_meta shape as the run-the-suite path
    so main() can call write_attested uniformly.
    """
    duration_ms = int((time.monotonic() - started) * 1000)
    result = {
        "status": "no_signal",
        # NOT True: a no-signal regression is not a satisfied gate.
        "passed": False,
        "total_tests": 0,
        "failed_count": 0,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "_attestation_meta": {
            "command": list(COMMAND_PREFIX),
            "exit_code": 0,
            "stdout": "",
            "stderr": message,
            "duration_ms": duration_ms,
        },
    }
    if branch is not None and head is not None:
        result["subject"] = {
            "branch": branch,
            "commit_sha": head,
        }
    return result


def _wrong_branch_result(
    started: float,
    *,
    branch: str | None,
    expected_branch: str,
    head: str | None = None,
) -> dict:
    """Build a result for "invoked on the wrong branch — suite not attempted".

    Distinct from no_signal: this is neither pass, fail, nor a legitimate
    empty-signal run. The correct consumer response is to check out
    ``expected_branch`` and re-run, so it exits 0 (never hard-fails a merge
    gate) but is NOT a satisfied gate (``passed`` is False).
    """
    duration_ms = int((time.monotonic() - started) * 1000)
    message = (
        f"Current branch is {branch}, expected {expected_branch}. "
        "Regression not run — check out the integration branch and re-run."
    )
    result = {
        "status": "skipped_wrong_branch",
        "passed": False,
        "expected_branch": expected_branch,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "_attestation_meta": {
            "command": list(COMMAND_PREFIX),
            "exit_code": 0,
            "stdout": "",
            "stderr": message,
            "duration_ms": duration_ms,
        },
    }
    if branch is not None and head is not None:
        result["subject"] = {"branch": branch, "commit_sha": head}
    return result


def _count_tests(stdout: str) -> int:
    """Best-effort count of total tests from output."""
    import re
    # pytest: "X passed" or "X passed, Y failed"
    match = re.search(r"(\d+) passed", stdout)
    if match:
        count = int(match.group(1))
        failed_match = re.search(r"(\d+) failed", stdout)
        if failed_match:
            count += int(failed_match.group(1))
        return count
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run cumulative regression suite")
    parser.add_argument("--project-path", type=Path, default=None)
    parser.add_argument(
        "--subject-branch", type=str, default=None,
        help="v2 (lean/sequential): the single build branch (e.g. build/<project>). "
             "If set, the run must prove it happened on this branch.",
    )
    parser.add_argument(
        "--wave", type=int, default=None,
        help="Legacy: wave number. If set (and --subject-branch is not), asserts "
             "the run happened on integration/wave-N.",
    )
    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(args.project_path)

    results = run_regression_suite(
        project_path, wave=args.wave, subject_branch=args.subject_branch,
    )

    write_attested_result(
        results,
        project_path / ".aah" / "build" / "test-results" / "regression-latest.json",
        project_path=project_path,
        command=COMMAND_PREFIX + sys.argv[1:],
        artifact_name="regression results",
    )

    json.dump(results, sys.stdout, indent=2)
    print()

    # Gate on `status` (source of truth), not `passed` alone. no_signal is
    # neither pass nor fail: it exits 0 (must not hard-FAIL a legitimate
    # early wave-0) but is NOT a satisfied gate (passed is False).
    status = results.get("status", "pass" if results.get("passed") else "fail")
    if status == "skipped_wrong_branch":
        print(
            f"Regression suite: SKIPPED — {results.get('message', 'wrong branch')}",
            file=sys.stderr,
        )
        sys.exit(0)
    elif status == "no_signal":
        print(
            f"Regression suite: NO SIGNAL — {results.get('message', 'no passing features to regress')}",
            file=sys.stderr,
        )
        sys.exit(0)
    elif status == "pass":
        print("Regression suite: ALL TESTS PASSED", file=sys.stderr)
        sys.exit(0)
    else:
        print("Regression suite: FAILURES DETECTED", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
