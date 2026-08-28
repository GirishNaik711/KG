#!/usr/bin/env python3
"""Rework trigger — current-wave rework model.

aah-fix owns feedback capture, in-model classification, and (via aah-plan) the
creation of superseding feature entries placed into the CURRENT wave (before it
promotes). By the time ``rework trigger`` runs, plan has already produced the
final ``affected_features`` list.

This module's job is deliberately small:
  - Reset the affected features (the rework entries) to ``passes: false`` so
    the normal wave engine re-builds them.
  - Bump the iteration counter and record rework context in the manifest.
  - Record an active-rework entry (with ``attempts`` for the retry cap) so the
    orchestrator can retire it once its features all pass, or escalate it to a
    human once it exceeds the cap.

It does NOT:
  - Re-classify feedback (aah-fix already classified in-model).
  - Recompute blast radius (aah-fix/plan already produced affected_features).
  - Reset ``current_wave`` backwards (the model never rewinds — the rework
    entries live in the current wave placed by plan).
  - Revert commits (superseded via ordinary forward merge; revert removed).

Usage:
    aah run core.build.rework trigger --project-path . --feedback-id UF-06-147 \
        --affected-features "F2-rework-01,F8"
    aah run core.build.rework reset-feature --project-path . --feature-id F003
    aah run core.build.rework status --project-path .
    aah run core.build.rework complete --project-path . --feedback-id UF-06-147
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.dag import dag_from_json, get_dependents
from aah.core.common.io_utils import read_json, read_yaml, write_json, write_yaml


# ---------------------------------------------------------------------------
# Trigger Rework
# ---------------------------------------------------------------------------


def trigger_rework(
    project_path: Path,
    feedback_id: str,
    affected_features_input: list[str] | None = None,
) -> dict:
    """Trigger a rework cycle for the features plan produced.

    Steps:
      1. Reset affected features (``passes: false``) so the wave engine rebuilds.
      2. Bump the manifest iteration counter + record rework history.
      3. Record an active-rework entry for the orchestrator to retire.

    The active-rework entry records ``attempts`` so the orchestrator's rework
    retry cap (``MAX_REWORK_ATTEMPTS``) can escalate a rework that keeps failing
    verify to a human instead of looping forever.
    """
    aah_path = project_path / ".aah"
    result = {
        "feedback_id": feedback_id,
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "actions_taken": [],
    }

    affected_features = sorted(set(affected_features_input)) if affected_features_input else []
    result["affected_features"] = affected_features

    # Reset affected features in feature-list.json to passes=false.
    fl_path = aah_path / "feature-list.json"
    if fl_path.exists() and affected_features:
        fl_data = read_json(fl_path)
        reset_count = 0
        for feature in fl_data.get("features", []):
            if feature.get("id") in affected_features and feature.get("passes", False):
                feature["passes"] = False
                reset_count += 1
        if reset_count > 0:
            write_json(fl_data, fl_path)
            result["actions_taken"].append(f"Reset {reset_count} features to passes=false")

    # Update status in source feature YAMLs.
    from aah.core.common.feature_list import _update_feature_yaml_status
    for fid in affected_features:
        _update_feature_yaml_status(aah_path, fid, "in_progress")

    # A rework supersedes already-approved code, so any standing user-review
    # approval is stale. Reset it so the checkpoint re-fires on the reworked
    # build instead of promoting code no human re-reviewed.
    for wave_num in _invalidate_wave_approvals(aah_path, feedback_id):
        result["actions_taken"].append(
            f"Invalidated wave-{wave_num} user-review approval (rework supersedes approved code)"
        )

    # Bump iteration counter + record rework context in manifest.
    manifest_path = aah_path / "manifest.yaml"
    iteration = 1
    if manifest_path.exists():
        manifest = read_yaml(manifest_path)
        current_iter = manifest.get("current_iteration", 1)
        iteration = current_iter + 1
        manifest["current_iteration"] = iteration

        if "rework_history" not in manifest:
            manifest["rework_history"] = []
        manifest["rework_history"].append({
            "iteration": iteration,
            "feedback_id": feedback_id,
            "affected_features": affected_features,
            "triggered_at": datetime.now(timezone.utc).isoformat(),
        })
        manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
        write_yaml(manifest, manifest_path)
        result["actions_taken"].append(f"Bumped iteration to {iteration}")

    # Record active-rework entry. The orchestrator retires the entry once those
    # features all reach passes=true. ``attempts`` tracks how many times this
    # feedback has re-entered the rebuild→verify loop; the orchestrator escalates
    # to a human once it exceeds the rework cap. Re-triggering the SAME feedback
    # (a failed verify looping back) bumps the existing entry's attempt count
    # instead of appending a duplicate.
    rework_state = _load_rework_state(aah_path)
    existing = next(
        (
            r for r in rework_state["active_reworks"]
            if r.get("feedback_id") == feedback_id and r.get("status") == "in_progress"
        ),
        None,
    )
    now = datetime.now(timezone.utc).isoformat()
    if existing is not None:
        existing["attempts"] = int(existing.get("attempts", 1)) + 1
        existing["affected_features"] = affected_features
        existing["iteration"] = iteration
        existing["last_triggered_at"] = now
        result["attempts"] = existing["attempts"]
        result["actions_taken"].append(
            f"Bumped rework attempts to {existing['attempts']} in state file"
        )
    else:
        rework_state["active_reworks"].append({
            "feedback_id": feedback_id,
            "iteration": iteration,
            "affected_features": affected_features,
            "attempts": 1,
            "status": "in_progress",
            "triggered_at": now,
        })
        result["attempts"] = 1
        result["actions_taken"].append("Recorded rework in state file")
    _save_rework_state(aah_path, rework_state)

    return result


# ---------------------------------------------------------------------------
# Reset Feature
# ---------------------------------------------------------------------------


def reset_feature(project_path: Path, feature_id: str, cascade: bool = False) -> dict:
    """Reset a single feature (and optionally dependents) for re-implementation."""
    aah_path = project_path / ".aah"
    result = {
        "feature_id": feature_id,
        "reset_features": [],
    }

    features_to_reset = [feature_id]
    if cascade:
        dag_path = aah_path / "plan" / "dag.json"
        if dag_path.exists():
            dag_data = read_json(dag_path)
            dag = dag_from_json(dag_data)
            dependents = get_dependents(dag, feature_id)
            features_to_reset = [feature_id] + dependents

    # Reset in feature-list.json
    fl_path = aah_path / "feature-list.json"
    if fl_path.exists():
        fl_data = read_json(fl_path)
        for feature in fl_data.get("features", []):
            if feature.get("id") in features_to_reset and feature.get("passes", False):
                feature["passes"] = False
                result["reset_features"].append(feature["id"])
        write_json(fl_data, fl_path)

    # Update status in source feature YAMLs
    from aah.core.common.feature_list import _update_feature_yaml_status
    for fid in result["reset_features"]:
        _update_feature_yaml_status(aah_path, fid, "in_progress")

    return result


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def get_rework_status(project_path: Path) -> dict:
    """Get current rework state and pending items."""
    aah_path = project_path / ".aah"
    state = _load_rework_state(aah_path)

    # Load manifest for iteration info
    manifest_path = aah_path / "manifest.yaml"
    iteration = 1
    if manifest_path.exists():
        manifest = read_yaml(manifest_path)
        iteration = manifest.get("current_iteration", 1)

    active = [r for r in state.get("active_reworks", []) if r.get("status") == "in_progress"]
    completed = [r for r in state.get("active_reworks", []) if r.get("status") in ("completed", "resolved")]

    return {
        "current_iteration": iteration,
        "active_reworks": len(active),
        "completed_reworks": len(completed),
        "active_details": active,
        "rework_history": state.get("active_reworks", []),
    }


# ---------------------------------------------------------------------------
# Complete Rework
# ---------------------------------------------------------------------------


def complete_rework(project_path: Path, feedback_id: str) -> dict:
    """Mark a rework cycle as completed."""
    aah_path = project_path / ".aah"
    state = _load_rework_state(aah_path)

    for rework in state.get("active_reworks", []):
        if rework.get("feedback_id") == feedback_id:
            rework["status"] = "completed"
            rework["completed_at"] = datetime.now(timezone.utc).isoformat()
            break

    _save_rework_state(aah_path, state)

    return {"feedback_id": feedback_id, "status": "completed"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invalidate_wave_approvals(aah_path: Path, feedback_id: str) -> list[int]:
    """Reset any standing user-review approval so the checkpoint re-fires.

    A rework supersedes already-approved code, so a prior ``approved: true`` is
    stale. Flip each approved ``wave-{N}-approval.json`` to ``approved: false``
    (kept for audit) — the orchestrator's ``_check_user_review_checkpoint`` then
    re-fires the user_review action for the reworked build instead of skipping
    to merge. Returns the wave numbers that were invalidated.
    """
    checkpoint_dir = aah_path / "build" / "checkpoint-results"
    invalidated: list[int] = []
    for approval_path in sorted(checkpoint_dir.glob("wave-*-approval.json")):
        approval = read_json(approval_path)
        if not approval.get("approved"):
            continue
        approval["approved"] = False
        approval["invalidated_by_rework"] = feedback_id
        write_json(approval, approval_path)
        invalidated.append(approval.get("wave"))
    return invalidated


def _load_rework_state(aah_path: Path) -> dict:
    """Load or create rework state file."""
    state_path = aah_path / "build" / "rework-state.json"
    if state_path.exists():
        return read_json(state_path)
    return {"active_reworks": []}


def _save_rework_state(aah_path: Path, state: dict) -> None:
    """Save rework state file."""
    state_path = aah_path / "build" / "rework-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(state, state_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="AAH Rework Cycle Management")
    sub = parser.add_subparsers(dest="command", required=True)

    trigger_p = sub.add_parser("trigger", help="Trigger rework from feedback")
    trigger_p.add_argument("--project-path", type=Path, default=None)
    trigger_p.add_argument("--feedback-id", type=str, required=True)
    trigger_p.add_argument("--affected-features", type=str, default=None,
                           help="Comma-separated feature IDs to rebuild (e.g. 'F2-rework-01,F8')")

    reset_p = sub.add_parser("reset-feature", help="Reset a feature for re-implementation")
    reset_p.add_argument("--project-path", type=Path, default=None)
    reset_p.add_argument("--feature-id", type=str, required=True)
    reset_p.add_argument("--cascade", action="store_true", help="Also reset dependents")

    status_p = sub.add_parser("status", help="Show rework status")
    status_p.add_argument("--project-path", type=Path, default=None)

    complete_p = sub.add_parser("complete", help="Mark rework as completed")
    complete_p.add_argument("--project-path", type=Path, default=None)
    complete_p.add_argument("--feedback-id", type=str, required=True)

    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(
        getattr(args, "project_path", None)
    )

    if args.command == "trigger":
        affected_input = None
        if getattr(args, "affected_features", None):
            affected_input = [f.strip() for f in args.affected_features.split(",") if f.strip()]
        result = trigger_rework(
            project_path, args.feedback_id,
            affected_features_input=affected_input,
        )
        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)
        json.dump(result, sys.stdout, indent=2)
        print()
        print(f"Rework triggered for {args.feedback_id}", file=sys.stderr)
        sys.exit(0)

    elif args.command == "reset-feature":
        result = reset_feature(project_path, args.feature_id, args.cascade)
        json.dump(result, sys.stdout, indent=2)
        print()
        print(f"Reset {len(result['reset_features'])} features", file=sys.stderr)
        sys.exit(0)

    elif args.command == "status":
        result = get_rework_status(project_path)
        json.dump(result, sys.stdout, indent=2)
        print()
        sys.exit(0)

    elif args.command == "complete":
        result = complete_rework(project_path, args.feedback_id)
        json.dump(result, sys.stdout, indent=2)
        print()
        sys.exit(0)


if __name__ == "__main__":
    main()
