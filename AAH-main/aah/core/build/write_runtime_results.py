#!/usr/bin/env python3
"""
Write attested runtime validation results for a wave.

Called by the aah-runtime-validator subagent after running its checks
(module validation, startup, smoke tests, health check). The agent
composes the consolidated checks blob in memory, then dispatches:

  aah run core.build.write_runtime_results \
    --wave N \
    --results-json '<full checks blob>' \
    --runtime-mode docker --port PORT \
    [--fix-category build|design_issue|user_required] \
    [--duration-ms N]

Writes to:
  .aah/build/runtime-results/wave-{N}-all.json

The writer — not the agent — decides the verdict. It validates the check
schema, derives ``overall_passed`` from the per-check booleans, and derives the
subject and runtime-criteria identity locally. An agent reports per-check state;
it cannot declare that the wave's runtime validation passed, cannot claim which
subject it evaluated, and cannot claim which criteria it was measured against.

The file is signed via attestation.write_attested so the orchestrator and
verify.py can confirm the file came from this script and was not forged.
Without that signature, a forged file with `"fix_category": "design_issue"`
could hijack the rework engine.

This mirrors the aah-qa-evaluator → write_qa_report subprocess pattern
that was already established for QA reports — the aah-runtime-validator
agent has no Write/Edit tools and persistence is delegated to this
deterministic writer.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.attestation import write_attested


# Canonical command prefix recorded in the attestation block. The
# orchestrator's runtime-results reader uses this exact prefix in
# expected_command_prefix to confirm the file came from this writer.
COMMAND_PREFIX = ["aah", "run", "core.build.write_runtime_results"]


_FIX_CATEGORIES = ("", "build", "design_issue", "user_required")

# Every runtime validation must report on all four. A payload with fewer is
# rejected — a partial payload cannot distinguish "this check passed" from
# "nobody looked". The agent emits all four on EVERY exit path, including an
# early failure, with unreached checks carrying passed=false (see
# aah/agents/aah-runtime-validator.md).
CORE_CHECKS = ("module_validation", "startup_validation", "smoke_tests", "health_check")

# Optional for frontend projects only.
OPTIONAL_CHECKS = ("ui_render",)


def _summarize(checks: dict) -> dict:
    """Compute summary counts from the checks blob."""
    total = len(checks)
    passed = sum(1 for v in checks.values() if isinstance(v, dict) and v.get("passed"))
    return {
        "total_checks": total,
        "passed": passed,
        "failed": total - passed,
    }


def validate_checks(checks: dict, wave: int) -> list[str]:
    """Return a list of schema problems; empty means the payload is valid.

    Rejects before writing so a malformed payload never becomes signed evidence.
    """
    problems: list[str] = []

    unknown = sorted(set(checks) - set(CORE_CHECKS) - set(OPTIONAL_CHECKS))
    if unknown:
        problems.append(f"unknown check key(s): {unknown}")

    missing = [k for k in CORE_CHECKS if k not in checks]
    if missing:
        problems.append(
            f"missing required check(s): {missing} — emit all of {list(CORE_CHECKS)} "
            "on every exit path, with unreached checks marked passed=false"
        )

    for key, value in sorted(checks.items()):
        if not isinstance(value, dict):
            problems.append(f"{key}: must be a mapping, got {type(value).__name__}")
            continue
        if value.get("check") != key:
            problems.append(f"{key}: check field is {value.get('check')!r}, expected {key!r}")
        if value.get("wave") != wave:
            problems.append(f"{key}: wave is {value.get('wave')!r}, expected {wave}")
        if not isinstance(value.get("passed"), bool):
            problems.append(f"{key}: passed must be a boolean, got {value.get('passed')!r}")
        if not value.get("timestamp"):
            problems.append(f"{key}: timestamp is required")
        if not isinstance(value.get("details"), dict):
            problems.append(f"{key}: details must be a mapping")

    return problems


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Write attested runtime validation results for a wave"
    )
    parser.add_argument("--wave", type=int, required=True)
    parser.add_argument(
        "--results-json",
        required=True,
        help=(
            "Full checks blob: JSON object with module_validation, "
            "startup_validation, smoke_tests, and health_check sub-objects, "
            "each containing {check, wave, timestamp, passed, details}. "
            "All four are required on every exit path."
        ),
    )
    parser.add_argument("--runtime-mode", default="docker")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--fix-category",
        default="",
        choices=_FIX_CATEGORIES,
        help=(
            "Failure routing for the orchestrator: 'build' (code bug, "
            "default), 'design_issue' (escalate to rework engine), "
            "'user_required' (block on user). Only consulted when the derived "
            "overall_passed is false."
        ),
    )
    parser.add_argument("--project-path", type=Path, default=None)
    parser.add_argument("--duration-ms", type=int, default=0)
    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(args.project_path)

    try:
        checks = json.loads(args.results_json)
    except json.JSONDecodeError as e:
        print(f"Error parsing --results-json: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(checks, dict):
        print(
            "Error: --results-json must be a JSON object mapping check names to "
            "result dicts. Got: " + type(checks).__name__,
            file=sys.stderr,
        )
        sys.exit(1)

    problems = validate_checks(checks, args.wave)
    if problems:
        print("Error: runtime check payload failed validation:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        sys.exit(1)

    # --- Preconditions: the writer proves what it measured -------------------
    from aah.core.common.git_utils import (
        AAH_STATE_PATHS,
        GitError,
        current_branch,
        porcelain_dirt,
    )
    from aah.core.build.verify import runtime_criteria_identity, subject_identity

    # Branch recorded, not asserted: the gates bind on subject.commit_sha.
    try:
        branch = current_branch(cwd=project_path)
    except GitError as e:
        print(f"Error: could not determine the current branch: {e}", file=sys.stderr)
        sys.exit(1)
    if porcelain_dirt(
        project_path, prefixes=AAH_STATE_PATHS, tracked_only=True
    ):
        print(
            "Error: the working tree has changes outside "
            f"{', '.join(AAH_STATE_PATHS)}. Runtime results would be bound to a "
            "subject nobody committed.",
            file=sys.stderr,
        )
        sys.exit(1)

    subject_sha = subject_identity(project_path)
    if subject_sha is None:
        print(
            f"Error: could not resolve the subject identity of {branch}.",
            file=sys.stderr,
        )
        sys.exit(1)

    # The verdict is DERIVED, never supplied. Summary counts agree with it by
    # construction because both read the same validated per-check booleans.
    overall_passed = all(bool(check["passed"]) for check in checks.values())

    fix_category = args.fix_category
    if overall_passed and fix_category:
        # Cosmetic, not fatal: fix_category is only consulted when the verdict is
        # false, and the derived verdict governs routing regardless. Rejecting
        # the write would leave no artifact on disk, so the orchestrator would
        # read MISSING and re-dispatch the validator forever.
        print(
            f"Warning: --fix-category {fix_category!r} was supplied but the derived "
            "verdict is a PASS; dropping the field.",
            file=sys.stderr,
        )
        fix_category = ""

    payload: dict = {
        "wave": args.wave,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "overall_passed": overall_passed,
        "runtime_mode": args.runtime_mode,
        "port": args.port,
        "checks": checks,
        "summary": _summarize(checks),
        # Derived locally — never accepted from the agent.
        "subject": {"branch": branch, "commit_sha": subject_sha},
        "runtime_criteria_sha256": runtime_criteria_identity(project_path, args.wave),
    }
    if fix_category:
        payload["fix_category"] = fix_category

    output_dir = project_path / ".aah" / "build" / "runtime-results"
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"wave-{args.wave}-all.json"

    write_attested(
        payload,
        out_path,
        project_path=project_path,
        command=COMMAND_PREFIX + sys.argv[1:],
        exit_code=0,
        stdout="",
        stderr="",
        duration_ms=args.duration_ms,
        artifact_name=f"wave-{args.wave} runtime validation",
    )

    # Human-readable summary to stderr (captured by subagent log).
    verdict = "PASS" if overall_passed else "FAIL"
    print(f"\n{'=' * 60}", file=sys.stderr)
    print(f"RUNTIME VALIDATION: wave {args.wave} — {verdict}", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)
    s = payload["summary"]
    print(f"Checks: {s['passed']}/{s['total_checks']} passed", file=sys.stderr)
    if not overall_passed:
        failed = [name for name, v in sorted(checks.items()) if not v.get("passed")]
        print(f"Failed checks: {', '.join(failed)}", file=sys.stderr)
        if fix_category:
            print(f"Fix category: {fix_category}", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)
    print(f"Results written to: {out_path}", file=sys.stderr)

    # JSON output for programmatic consumption.
    json.dump(payload, sys.stdout, indent=2)
    print()
    sys.exit(0)


if __name__ == "__main__":
    main()
