#!/usr/bin/env python3
"""User-review checkpoints and feature-QA human decisions.

This module owns the HUMAN side of a wave: whether a wave has a user-review
checkpoint, the demo context presented for it, the recorded approval, and the
per-feature QA approve/rework decisions.

It does NOT consolidate technical evidence. The former ``run-system``
subcommand reread signed runtime, regression, standards, and spec evidence and
wrote a wrapper verdict without executing a single check; ``verify.py``
authenticates and evaluates that evidence directly, so the wrapper was pure
duplication. The mandatory runtime/system validation itself is unaffected — the
orchestrator's ``run_system_checkpoint`` action still dispatches the
aah-runtime-validator agent, which writes signed
``build/runtime-results/wave-<N>-all.json``.

Subcommands:
    is-user-checkpoint — Check if a wave requires user review
    prepare-demo      — Prepare demo context for user review
    approve           — Record user approval of a wave checkpoint
    status            — Show current checkpoint state

Usage:
    aah run core.build.validate_checkpoint status --project-path . --wave 0
    aah run core.build.validate_checkpoint approve --project-path . --wave 0
    aah run core.build.validate_checkpoint is-user-checkpoint --project-path . --wave 0
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.build.verification_evidence import RUNTIME_RESULTS_PREFIXES
from aah.core.common.audit import append_gate_decision
from aah.core.common.io_utils import read_json, read_yaml, write_json
from aah.core.common.progress import add_checkpoint_result
from aah.core.common.sequenced_store import claim_sequenced_path
from aah.core.common.verified_artifacts import ArtifactState, load_attested_artifact


# ---------------------------------------------------------------------------
# User Review Checkpoint Helpers
# ---------------------------------------------------------------------------


def is_user_review_checkpoint(project_path: Path, wave: int) -> dict:
    """Check if a user review checkpoint should occur after this wave."""
    aah_path = project_path / ".aah"
    config_path = aah_path / "plan" / "checkpoint-config.yaml"

    if not config_path.exists():
        return {"is_user_checkpoint": False, "reason": "No checkpoint config found"}

    config = read_yaml(config_path)
    checkpoints = (config.get("checkpoint_configuration", {})
                   .get("user_review_checkpoints", []))

    for cp in checkpoints:
        if cp.get("after_wave") == wave:
            return {
                "is_user_checkpoint": True,
                "checkpoint_id": cp.get("id"),
                "demo_checklist": cp.get("demo_checklist", []),
                "feedback_questions": cp.get("feedback_questions", []),
                "success_criteria": cp.get("success_criteria", []),
            }

    return {"is_user_checkpoint": False, "reason": f"Wave {wave} is not a user review checkpoint"}


def prepare_user_demo(project_path: Path, wave: int) -> dict:
    """Prepare demo context for user review checkpoint."""
    aah_path = project_path / ".aah"

    # Get checkpoint config
    cp_info = is_user_review_checkpoint(project_path, wave)
    if not cp_info.get("is_user_checkpoint"):
        return {"error": f"Wave {wave} is not a user review checkpoint"}

    # Gather implemented features up to this wave
    waves_path = aah_path / "plan" / "waves.json"
    completed_features = []
    if waves_path.exists():
        waves_data = read_json(waves_path)
        from aah.core.plan.compute_waves import flatten_waves
        waves = flatten_waves(waves_data)
        for w_idx in range(wave + 1):
            if w_idx < len(waves):
                completed_features.extend(waves[w_idx])

    # Load feature descriptions
    features_dir = aah_path / "plan" / "features"
    feature_details = []
    for fid in completed_features:
        from aah.core.common.feature_utils import load_feature_data
        if features_dir.is_dir():
            data = load_feature_data(features_dir, fid)
            if data:
                feature_details.append({
                    "id": fid,
                    "name": data.get("name", fid),
                    "description": data.get("description", ""),
                })
            break

    demo_context = {
        "checkpoint_id": cp_info.get("checkpoint_id"),
        "wave": wave,
        "features_to_demo": feature_details,
        "demo_checklist": cp_info.get("demo_checklist", []),
        "feedback_questions": cp_info.get("feedback_questions", []),
        "prepared_at": datetime.now(timezone.utc).isoformat(),
    }

    # Write demo context
    checkpoint_dir = aah_path / "build" / "checkpoint-results"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    write_json(demo_context, checkpoint_dir / f"wave-{wave}-demo-context.json")

    return demo_context


# ---------------------------------------------------------------------------
# Feature QA Human Decisions
# ---------------------------------------------------------------------------


def _allocate_human_decision(project_path: Path, feature_id: str) -> tuple[int, Path]:
    """Allocate next gap-free human decision number and return its path.

    Mirrors qa_evidence.allocate_attempt's O_CREAT|O_EXCL atomic claim over
    human-*.json under .aah/build/qa-results/FXXX/. Never overwrites.

    Args:
        project_path: Project root containing .aah/
        feature_id: Feature identifier (e.g., "F001")

    Returns:
        (decision_number, decision_path) where decision_number is 1-indexed
        and decision_path is the absolute path to human-NNN.json

    Raises:
        RuntimeError: If unable to allocate after reasonable attempts
    """
    return claim_sequenced_path(
        project_path / ".aah" / "build" / "qa-results" / feature_id,
        "human-",
    )


def feature_qa_approve(
    project_path: Path,
    feature_id: str,
) -> dict:
    """Record the user's approval choice without re-validating QA evidence."""
    return _record_feature_qa_decision(project_path, feature_id, "approve")


def feature_qa_request_rework(
    project_path: Path,
    feature_id: str,
    rationale: str,
) -> dict:
    """Record the user's rework choice and feedback."""
    return _record_feature_qa_decision(
        project_path, feature_id, "request_rework", rationale=rationale
    )


def _record_feature_qa_decision(
    project_path: Path,
    feature_id: str,
    decision: str,
    *,
    rationale: str | None = None,
) -> dict:
    """Persist one prompt response for the latest QA attempt as plain JSON."""
    from aah.core.build.qa_evidence import latest_attempt

    aah_path = project_path / ".aah"
    latest = latest_attempt(project_path, feature_id) or {}
    _decision_num, decision_path = _allocate_human_decision(project_path, feature_id)
    record = {
        "feature_id": feature_id,
        "attempt": latest.get("attempt"),
        "decision": decision,
        "recorded_by": "user",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if rationale:
        record["rationale"] = rationale
    write_json(record, decision_path)
    append_gate_decision(
        aah_path,
        gate=f"feature_qa_{decision}",
        feature_id=feature_id,
        decision=decision,
        reviewer="user",
    )
    return record


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


def approve_checkpoint(project_path: Path, wave: int, approved_by: str = "user") -> dict:
    """Mark a wave checkpoint as approved."""
    aah_path = project_path / ".aah"

    approval = {
        "wave": wave,
        "approved": True,
        "approved_by": approved_by,
        "approved_at": datetime.now(timezone.utc).isoformat(),
    }

    checkpoint_dir = aah_path / "build" / "checkpoint-results"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    write_json(approval, checkpoint_dir / f"wave-{wave}-approval.json")

    # Record in progress
    progress_path = aah_path / "claude-progress.json"
    if progress_path.exists():
        cp_type = "user_review" if is_user_review_checkpoint(project_path, wave).get("is_user_checkpoint") else "system"
        add_checkpoint_result(
            progress_path,
            checkpoint_id=f"APPROVED-W{wave}",
            checkpoint_type=cp_type,
            wave=wave,
            passed=True,
            details={"approved_by": approved_by},
        )

    return approval


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def get_checkpoint_status(project_path: Path, wave: int) -> dict:
    """Get the current status of a wave's checkpoint."""
    aah_path = project_path / ".aah"
    checkpoint_dir = aah_path / "build" / "checkpoint-results"

    status = {
        "wave": wave,
        "system_checkpoint": None,
        "is_user_checkpoint": False,
        "user_demo_prepared": False,
        "approved": False,
    }

    # The signed runtime result is the system-checkpoint authority. Historical
    # wrapper files remain audit-only and are never consulted.
    runtime_file = aah_path / "build" / "runtime-results" / f"wave-{wave}-all.json"
    artifact = load_attested_artifact(
        runtime_file, project_path, RUNTIME_RESULTS_PREFIXES, "json"
    )
    if artifact.state is ArtifactState.VERIFIED:
        sys_data = artifact.payload or {}
        status["system_checkpoint"] = {
            "passed": sys_data.get("overall_passed"),
            "blocking_issues": sum(
                1
                for check in (sys_data.get("checks") or {}).values()
                if isinstance(check, dict) and check.get("passed") is not True
            ),
            "timestamp": sys_data.get("timestamp"),
        }

    # User checkpoint
    cp_info = is_user_review_checkpoint(project_path, wave)
    status["is_user_checkpoint"] = cp_info.get("is_user_checkpoint", False)

    # Demo prepared
    demo_file = checkpoint_dir / f"wave-{wave}-demo-context.json"
    status["user_demo_prepared"] = demo_file.exists()

    # Approval
    approval_file = checkpoint_dir / f"wave-{wave}-approval.json"
    if approval_file.exists():
        approval = read_json(approval_file)
        status["approved"] = approval.get("approved", False)
        status["approved_at"] = approval.get("approved_at")

    return status


# ---------------------------------------------------------------------------
# CLI Formatting
# ---------------------------------------------------------------------------

_BANNER_LINE = "\u2550" * 61
_OK, _NO, _DASH = "\u2705", "\u274c", "\u2014"


def _banner(title: str, body: list[str]) -> str:
    """Wrap body lines in the standard header/footer banner (stderr)."""
    return "\n".join(["", _BANNER_LINE, f"  {title}", _BANNER_LINE, *body, _BANNER_LINE])


def _verdict(passed, *, ok_suffix: str = "", fail_suffix: str = "") -> str:
    """PASS/FAIL/NOT-RUN status label. ``passed is None`` renders NOT RUN."""
    if passed is None:
        return f"{_DASH} NOT RUN"
    return f"{_OK} PASS{ok_suffix}" if passed else f"{_NO} FAIL{fail_suffix}"


def _format_prepare_demo_result(result: dict) -> str:
    """Format user demo preparation result as a CLI banner for stderr."""
    wave = result.get("wave", "?")
    checkpoint_id = result.get("checkpoint_id", "")
    features = result.get("features_to_demo", [])
    checklist = result.get("demo_checklist", [])
    questions = result.get("feedback_questions", [])

    body = ["  Demo prepared for user review.", "", f"  Features to demo ({len(features)}):"]
    for feat in features:
        desc = feat.get("description", feat.get("name", ""))
        if len(desc) > 50:
            desc = desc[:47] + "..."
        body.append(f"    {feat.get('id', '?')}: {desc}")

    if checklist:
        body.append("")
        body.append("  Demo checklist:")
        body.extend(f"    [ ] {item}" for item in checklist)

    if questions:
        body.append("")
        body.append("  Feedback questions:")
        body.extend(f"    {i}. {q}" for i, q in enumerate(questions, 1))

    body.append("")
    body.append(f"  To approve: aah run core.build.validate_checkpoint approve --wave {wave}")
    return _banner(f"USER REVIEW CHECKPOINT \u2014 Wave {wave} ({checkpoint_id})", body)


def _format_approval_result(result: dict) -> str:
    """Format checkpoint approval as a CLI banner for stderr."""
    wave = result.get("wave", "?")
    return _banner(
        f"CHECKPOINT APPROVED \u2014 Wave {wave} {_OK}",
        [
            f"  Approved by:  {result.get('approved_by', 'user')}",
            f"  Approved at:  {result.get('approved_at', '')}",
            "",
            f"  Wave {wave} is now cleared for merge/promotion.",
        ],
    )


def _format_status_result(result: dict) -> str:
    """Format checkpoint status as a CLI banner for stderr."""
    wave = result.get("wave", "?")
    sys_cp = result.get("system_checkpoint")
    is_user = result.get("is_user_checkpoint", False)
    demo_ready = result.get("user_demo_prepared", False)
    approved = result.get("approved", False)

    if sys_cp:
        blocking = sys_cp.get("blocking_issues", 0)
        sys_line = _verdict(sys_cp.get("passed"), ok_suffix=" (0 blocking)", fail_suffix=f" ({blocking} blocking)")
    else:
        sys_line = f"{_DASH} NOT RUN"
    body = [f"  System Checkpoint:   {sys_line}"]

    if is_user:
        body.append("  User Review:         Required")
        body.append(f"  Demo Prepared:       {_OK if demo_ready else _NO} {'Yes' if demo_ready else 'No'}")
    else:
        body.append("  User Review:         Not required for this wave")

    if approved:
        body.append(f"  Approved:            {_OK} Yes ({result.get('approved_at', '')})")
    else:
        body.append(f"  Approved:            {_NO} No")

    return _banner(f"CHECKPOINT STATUS \u2014 Wave {wave}", body)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="AAH Checkpoint Validation")
    sub = parser.add_subparsers(dest="command", required=True)

    is_user_p = sub.add_parser("is-user-checkpoint", help="Check if wave has user review")
    is_user_p.add_argument("--project-path", type=Path, default=None)
    is_user_p.add_argument("--wave", type=int, required=True)

    demo_p = sub.add_parser("prepare-demo", help="Prepare user demo context")
    demo_p.add_argument("--project-path", type=Path, default=None)
    demo_p.add_argument("--wave", type=int, required=True)

    approve_p = sub.add_parser("approve", help="Approve checkpoint")
    approve_p.add_argument("--project-path", type=Path, default=None)
    approve_p.add_argument("--wave", type=int, required=True)
    approve_p.add_argument("--approved-by", type=str, default="user")

    status_p = sub.add_parser("status", help="Get checkpoint status")
    status_p.add_argument("--project-path", type=Path, default=None)
    status_p.add_argument("--wave", type=int, required=True)

    approve_qa_p = sub.add_parser("feature-qa-approve", help="Record feature QA approval")
    approve_qa_p.add_argument("--project-path", type=Path, default=None)
    approve_qa_p.add_argument("--feature-id", required=True)

    rework_qa_p = sub.add_parser("feature-qa-request-rework", help="Request rework for feature QA")
    rework_qa_p.add_argument("--project-path", type=Path, default=None)
    rework_qa_p.add_argument("--feature-id", required=True)
    rework_qa_p.add_argument("--rationale", required=True)

    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(
        getattr(args, "project_path", None)
    )

    if args.command == "is-user-checkpoint":
        result = is_user_review_checkpoint(project_path, args.wave)
        json.dump(result, sys.stdout, indent=2)
        print()
        is_user = result.get("is_user_checkpoint", False)
        if is_user:
            cp_id = result.get("checkpoint_id", "")
            print(f"Wave {args.wave} IS a user review checkpoint ({cp_id})", file=sys.stderr)
        else:
            print(f"Wave {args.wave} is NOT a user review checkpoint", file=sys.stderr)
        sys.exit(0)

    elif args.command == "prepare-demo":
        result = prepare_user_demo(project_path, args.wave)
        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)
        json.dump(result, sys.stdout, indent=2)
        print()
        print(_format_prepare_demo_result(result), file=sys.stderr)
        sys.exit(0)

    elif args.command == "approve":
        result = approve_checkpoint(project_path, args.wave, args.approved_by)
        json.dump(result, sys.stdout, indent=2)
        print()
        print(_format_approval_result(result), file=sys.stderr)
        sys.exit(0)

    elif args.command == "status":
        result = get_checkpoint_status(project_path, args.wave)
        json.dump(result, sys.stdout, indent=2)
        print()
        print(_format_status_result(result), file=sys.stderr)
        sys.exit(0)

    elif args.command == "feature-qa-approve":
        result = feature_qa_approve(
            project_path,
            args.feature_id,
        )
        json.dump(result, sys.stdout, indent=2)
        print()
        print(
            f"✓ Feature {args.feature_id} approved by user",
            file=sys.stderr,
        )
        sys.exit(0)

    elif args.command == "feature-qa-request-rework":
        result = feature_qa_request_rework(
            project_path,
            args.feature_id,
            args.rationale,
        )
        json.dump(result, sys.stdout, indent=2)
        print()
        print(
            f"✓ Rework requested for {args.feature_id} by user",
            file=sys.stderr,
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
