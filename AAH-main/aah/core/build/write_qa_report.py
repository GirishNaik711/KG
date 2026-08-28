#!/usr/bin/env python3
"""
Write a structured QA evaluation report for a feature.

Called by the aah-qa-evaluator subagent after completing its evaluation:
  aah run core.build.write_qa_report \
    --feature-id FXXX \
    --verdict pass|rework_required|human_review_required \
    --criteria-json '[{"id":"AC1","description":"...","verdict":"pass","evidence":"..."},...]' \
    --issues-json '[{"issue_id":"...","affected_ac_ids":[...],"affected_tc_ids":[...],"severity":"critical|major|minor","evidence":"...","requested_behavior":"..."},...]' \
    [--test-command "uv run pytest ..."] \
    [--tests-run N] \
    [--tests-passed N]

Writes authoritative evidence to:
  .aah/build/qa-results/FXXX/attempt-NNN.json
  .aah/build/qa-results/FXXX/attempt-NNN-tests.json

Also prints a human-readable summary to stderr so it appears in the subagent log.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aah.core.build.evidence import (
    EvidenceError,
    capture_subject,
    subject_matches_binding,
)
from aah.core.common.attestation import write_attested


# Canonical command prefix recorded in authoritative QA attempt attestations.
COMMAND_PREFIX = ["aah", "run", "core.build.write_qa_report"]


def main() -> None:
    started = time.monotonic()
    parser = argparse.ArgumentParser(description="Write QA evaluation report")
    parser.add_argument("--feature-id", required=True)
    parser.add_argument("--verdict", required=True, choices=["pass", "rework_required", "human_review_required"])
    parser.add_argument(
        "--criteria-json",
        required=True,
        help="JSON array of criteria results: [{id, description, verdict, evidence}, ...]",
    )
    parser.add_argument(
        "--issues-json",
        default="[]",
    )
    parser.add_argument("--test-command", default="")
    parser.add_argument("--tests-run", type=int, default=0)
    parser.add_argument("--tests-passed", type=int, default=0)
    parser.add_argument("--project-path", type=Path, default=None)
    parser.add_argument("--subject-path", type=Path, required=True)
    parser.add_argument("--subject-branch", required=True)
    parser.add_argument("--subject-sha", default=None)
    parser.add_argument("--actor", default="qa", help="Actor identifier (default: qa)")
    parser.add_argument(
        "--tests-json",
        default="",
        help="Optional JSON blob of reviewed test evidence for attempt-NNN-tests.json",
    )
    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(args.project_path)

    try:
        subject = capture_subject(args.subject_path, project_path)
    except EvidenceError as exc:
        print(f"ERROR: Cannot capture QA subject: {exc}", file=sys.stderr)
        sys.exit(2)
    # The SHA is captured after implementation when the caller omits it. This
    # avoids comparing QA against the pre-implementation dispatch SHA while
    # still honoring an explicitly supplied exact binding.
    expected_sha = args.subject_sha or subject.get("commit_sha")
    if not subject_matches_binding(
        subject,
        expected_branch=args.subject_branch,
        expected_sha=expected_sha,
    ):
        print(
            "ERROR: QA subject is dirty or does not match --subject-branch "
            "or the supplied --subject-sha.",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        criteria = json.loads(args.criteria_json)
    except json.JSONDecodeError as e:
        print(f"Error parsing criteria JSON: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        issues = json.loads(args.issues_json)
    except json.JSONDecodeError:
        issues = []

    passed_criteria = [c for c in criteria if c.get("verdict") == "pass"]
    failed_criteria = [c for c in criteria if c.get("verdict") == "fail"]

    # No Tier-1 spec-validation re-gate here. The structural AC-to-test join now
    # runs at planning time (validate_feature_contract) and inside the scoped
    # feature-test run (missing planned test cases fail that run). QA owns the
    # semantic verdict only.
    #
    # Every other guard on --verdict pass is unchanged: attestation, the subject
    # binding checked above, append-only attempt allocation, and the criteria
    # roll-up below.

    report = {
        "feature_id": args.feature_id,
        "verdict": args.verdict,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "criteria_total": len(criteria),
            "criteria_passed": len(passed_criteria),
            "criteria_failed": len(failed_criteria),
            "tests_run": args.tests_run,
            "tests_passed": args.tests_passed,
        },
        "criteria": criteria,
        "issues": issues,
        "test_command": args.test_command,
        "subject": subject,
    }

    # Write the append-only authoritative attempt history.
    from aah.core.build import qa_evidence

    # Snapshot verification routing into the attempt. Later checkpoint-config
    # edits do not change the checks selected for this tested subject.
    subject_sha_for_binding = subject["commit_sha"]
    verification_profile = qa_evidence.profile_binding_for_feature(
        project_path, args.feature_id, subject_sha=subject_sha_for_binding
    )
    report["verification_profile"] = verification_profile

    # Allocate next gap-free attempt number
    attempt_no, attempt_path = qa_evidence.allocate_attempt(project_path, args.feature_id)

    # Build authoritative attempt payload
    duration = int((time.monotonic() - started) * 1000)
    attempt_payload = {
        "schema_version": 1,
        "feature_id": args.feature_id,
        "attempt": attempt_no,
        "verdict": args.verdict,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "subject": subject,
        "test_command": args.test_command,
        "criteria": criteria,
        "issues": issues,
        "summary": report["summary"],
        "verification_profile": verification_profile,
        "tests_evidence": f"attempt-{attempt_no:03d}-tests.json",
    }

    # Write authoritative attempt-NNN.json FIRST (so write_attempt_tests can read it)
    write_attested(
        attempt_payload,
        attempt_path,
        project_path=project_path,
        command=COMMAND_PREFIX + sys.argv[1:],
        exit_code=0,
        stdout="",
        stderr="",
        duration_ms=duration,
        artifact_name=f"{args.feature_id} QA attempt {attempt_no}",
    )

    # Parse tests-json if provided, or build from scalar test counts
    tests_payload: dict[str, Any] = {}
    if args.tests_json:
        try:
            tests_payload = json.loads(args.tests_json)
        except json.JSONDecodeError as e:
            print(f"Warning: Failed to parse --tests-json: {e}", file=sys.stderr)
            tests_payload = {}

    # Fallback: if tests-json empty but we have test counts, build minimal payload
    if not tests_payload and (args.tests_run > 0 or args.test_command):
        tests_payload = {
            "tests_run": args.tests_run,
            "tests_passed": args.tests_passed,
            "test_command": args.test_command,
        }

    # Write attempt-NNN-tests.json (subject-bound reviewed test evidence)
    # Always write attempt-NNN-tests.json so every attempt has an artifact
    qa_evidence.write_attempt_tests(
        project_path,
        args.feature_id,
        attempt_no,
        tests_payload,
        command=COMMAND_PREFIX + sys.argv[1:],
        duration_ms=duration,
    )

    # Update qa_status in feature file and sync to feature-list.json
    features_dir = project_path / ".aah" / "plan" / "features"
    if features_dir.is_dir():
        from aah.core.common.feature_utils import find_feature_file, parse_feature_frontmatter, write_feature_frontmatter
        feature_file = find_feature_file(features_dir, args.feature_id)
        if feature_file and feature_file.exists():
            data = parse_feature_frontmatter(feature_file)
            if data and data.get("id") == args.feature_id:
                data["qa_status"] = "passed" if args.verdict == "pass" else "failed"
                write_feature_frontmatter(data, feature_file)
        # Sync to feature-list.json immediately
        fl_path = project_path / ".aah" / "feature-list.json"
        if fl_path.exists():
            try:
                from aah.core.common.feature_list import sync_features_from_yaml
                sync_features_from_yaml(features_dir, fl_path, feature_ids=[args.feature_id])
            except Exception:
                pass

    # When QA passes, mark feature as passing in feature-list.json
    if args.verdict == "pass":
        fl_path = project_path / ".aah" / "feature-list.json"
        if fl_path.exists():
            from aah.core.common.feature_list import update_feature_status
            update_feature_status(fl_path, args.feature_id, passes=True)

    # Human-readable summary to stderr (captured by subagent log)
    verdict_icon = args.verdict.upper()
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"QA REPORT: {args.feature_id} — {verdict_icon}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"Criteria: {len(passed_criteria)}/{len(criteria)} passed", file=sys.stderr)
    if args.tests_run > 0:
        print(f"Tests:    {args.tests_passed}/{args.tests_run} passed", file=sys.stderr)
    if failed_criteria:
        print(f"\nFAILED CRITERIA:", file=sys.stderr)
        for c in failed_criteria:
            print(f"  [{c.get('id','?')}] {c.get('description','')}", file=sys.stderr)
            print(f"       Evidence: {c.get('evidence','no evidence provided')}", file=sys.stderr)
    if issues:
        print(f"\nISSUES TO FIX ({len(issues)} total):", file=sys.stderr)
        for i, issue in enumerate(issues, 1):
            sev = issue.get("severity", "").upper()
            print(f"  {i}. [{sev}] {issue.get('issue_id', issue.get('issue', ''))}", file=sys.stderr)
            if issue.get("requested_behavior"):
                print(f"     Requested behavior: {issue.get('requested_behavior')}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"Attempt written to: {attempt_path}", file=sys.stderr)

    # JSON output for programmatic consumption
    json.dump(report, sys.stdout, indent=2)
    print()
    sys.exit(0)


if __name__ == "__main__":
    main()
