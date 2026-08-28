#!/usr/bin/env python3
"""Read/write/snapshot claude-progress.json — the progress journal."""

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.io_utils import read_json, write_json


PROGRESS_FILENAME = "claude-progress.json"


def get_default_progress() -> dict:
    """Return a default progress structure for a new project."""
    return {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "current_phase": "init",
        "current_wave": None,
        "current_tier": 0,
        "last_completed_feature": None,
        "in_progress_features": [],
        "known_issues": [],
        "environment_state": "unknown",
        "next_steps": "Initialize project and begin problem statement intake",
        "session_history": [],
        "checkpoint_history": [],
    }


def find_progress(start_dir: Path | None = None) -> Path | None:
    """Find claude-progress.json. If start_dir given, walk up. Otherwise use config."""
    if start_dir:
        for directory in [start_dir, *start_dir.parents]:
            candidate = directory / ".aah" / PROGRESS_FILENAME
            if candidate.exists():
                return candidate
        return None
    from aah.core.common.config import get_active_project_aah_path
    aah_path = get_active_project_aah_path()
    if aah_path:
        candidate = aah_path / PROGRESS_FILENAME
        if candidate.exists():
            return candidate
    return None


def _without_timestamp(data: dict) -> dict:
    """Return `data` minus the bookkeeping-only `last_updated` field.

    Every other key is compared, so any header change (phase, wave, tier,
    issues, next_steps, ...) still counts as a real change — including keys
    added to the schema later.
    """
    return {k: v for k, v in data.items() if k != "last_updated"}


def is_duplicate_session_entry(
    history: list, summary: str, features_completed: list[str] | None = None
) -> bool:
    """True if appending this entry would repeat the last one verbatim.

    A session_history entry carries only ``summary`` and ``features_completed``
    beyond its timestamp, so matching on both makes the new entry
    indistinguishable to a reader. Compared against the last entry only: a
    genuine return to an earlier state after intermediate ones is real history.
    """
    if not history:
        return False
    last = history[-1]
    return (
        last.get("summary") == summary
        and (last.get("features_completed") or []) == (features_completed or [])
    )


def load_progress(progress_path: Path | None = None) -> dict:
    """Load claude-progress.json from the given path or by searching."""
    if progress_path is None:
        progress_path = find_progress()
    if progress_path is None or not progress_path.exists():
        return get_default_progress()
    return read_json(progress_path)


def save_progress(data: dict, progress_path: Path, force: bool = False) -> None:
    """Save claude-progress.json, updating the timestamp.

    No-op when every field except ``last_updated`` already matches what is on
    disk. This file is git-tracked and written from ~10 call sites, several of
    which write the value they just read (e.g. ``update_progress(wave=N)`` when
    already at wave N). Restamping ``last_updated`` on those turned no-op calls
    into real diffs, leaving the file dirty and blocking ``git checkout``.

    ``force=True`` writes unconditionally.
    """
    if not force and progress_path.exists():
        try:
            existing = read_json(progress_path)
            if _without_timestamp(existing) == _without_timestamp(data):
                return
        except Exception:
            pass  # unreadable/corrupt — fall through and write
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    write_json(data, progress_path)


def update_progress(
    progress_path: Path,
    phase: str | None = None,
    wave: int | None = None,
    tier: int | None = None,
    completed_feature: str | None = None,
    in_progress: list[str] | None = None,
    issue: str | None = None,
    remove_issue: str | None = None,
    env_state: str | None = None,
    next_steps: str | None = None,
) -> dict:
    """Update specific fields in the progress journal."""
    data = load_progress(progress_path)

    if phase is not None:
        data["current_phase"] = phase
    if wave is not None:
        current = data.get("current_wave") or 0
        if wave < current:
            # Wave must always advance sequentially — silently skip backward write
            pass
        else:
            # A bare wave advance must NOT silently zero current_tier: an
            # unrelated wave-write (e.g. orchestrator re-entry to the first
            # incomplete wave) would otherwise reset an in-progress tier back to
            # 0 and trigger a spurious advance_tier. Callers that genuinely start
            # a fresh wave pass tier=0 explicitly (see promote_to_develop).
            data["current_wave"] = wave
    if tier is not None:
        data["current_tier"] = tier
    if completed_feature is not None:
        data["last_completed_feature"] = completed_feature
        # Remove from in-progress if present
        if completed_feature in data.get("in_progress_features", []):
            data["in_progress_features"].remove(completed_feature)
    if in_progress is not None:
        data["in_progress_features"] = in_progress
    if issue is not None:
        if "known_issues" not in data:
            data["known_issues"] = []
        if issue not in data["known_issues"]:
            data["known_issues"].append(issue)
    if remove_issue is not None and remove_issue in data.get("known_issues", []):
        data["known_issues"].remove(remove_issue)
    if env_state is not None:
        data["environment_state"] = env_state
    if next_steps is not None:
        data["next_steps"] = next_steps

    save_progress(data, progress_path)
    return data


def add_structured_issue(
    progress_path: Path,
    issue_id: str,
    description: str,
    category: str = "unknown",
    severity: str = "medium",
    detected_during: str | None = None,
    detected_at_wave: int | None = None,
    root_cause_phase: str | None = None,
    root_cause_artifact: str | None = None,
    affected_features: list[str] | None = None,
) -> dict:
    """Add a structured issue to the known_issues list.

    Supports both legacy string format and new structured format.
    """
    data = load_progress(progress_path)
    if "known_issues" not in data:
        data["known_issues"] = []

    issue_entry = {
        "id": issue_id,
        "description": description,
        "category": category,
        "severity": severity,
        "detected_during": detected_during,
        "detected_at_wave": detected_at_wave,
        "root_cause_phase": root_cause_phase,
        "root_cause_artifact": root_cause_artifact,
        "affected_features": affected_features or [],
        "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # Avoid duplicates by id
    existing_ids = [
        i.get("id") if isinstance(i, dict) else None
        for i in data["known_issues"]
    ]
    if issue_id not in existing_ids:
        data["known_issues"].append(issue_entry)

    save_progress(data, progress_path)
    return data


def resolve_issue(progress_path: Path, issue_id: str, resolution: str = "fixed") -> dict:
    """Mark a structured issue as resolved."""
    data = load_progress(progress_path)
    for issue in data.get("known_issues", []):
        if isinstance(issue, dict) and issue.get("id") == issue_id:
            issue["status"] = "resolved"
            issue["resolution"] = resolution
            issue["resolved_at"] = datetime.now(timezone.utc).isoformat()
            break
    save_progress(data, progress_path)
    return data


def add_checkpoint_result(
    progress_path: Path,
    checkpoint_id: str,
    checkpoint_type: str,
    wave: int,
    passed: bool,
    details: dict | None = None,
) -> dict:
    """Record a checkpoint result in the checkpoint history.

    checkpoint_type: "system" | "user_review"
    """
    data = load_progress(progress_path)
    if "checkpoint_history" not in data:
        data["checkpoint_history"] = []

    entry = {
        "checkpoint_id": checkpoint_id,
        "type": checkpoint_type,
        "wave": wave,
        "passed": passed,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "details": details or {},
    }
    data["checkpoint_history"].append(entry)
    save_progress(data, progress_path)
    return data


def get_open_issues(progress_path: Path) -> list[dict]:
    """Get all open (unresolved) structured issues."""
    data = load_progress(progress_path)
    issues = []
    for issue in data.get("known_issues", []):
        if isinstance(issue, dict) and issue.get("status") == "open":
            issues.append(issue)
        elif isinstance(issue, str):
            # Legacy format: treat as open
            issues.append({"id": None, "description": issue, "status": "open"})
    return issues


def add_session_entry(
    progress_path: Path,
    summary: str,
    features_completed: list[str] | None = None,
    allow_duplicate: bool = False,
) -> dict:
    """Append a session summary entry to the session history.

    Skips the append when it would repeat the previous entry verbatim — the
    
    Returns the unchanged data on skip, so callers still read current state.
    ``allow_duplicate=True`` forces the append.
    """
    data = load_progress(progress_path)
    if "session_history" not in data:
        data["session_history"] = []
    if not allow_duplicate and is_duplicate_session_entry(
        data["session_history"], summary, features_completed
    ):
        return data
    data["session_history"].append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "features_completed": features_completed or [],
    })
    save_progress(data, progress_path)
    return data


def snapshot(progress_path: Path, snapshot_dir: Path | None = None) -> Path:
    """Take a snapshot of current progress (used before context compaction)."""
    data = load_progress(progress_path)
    snapshot_data = copy.deepcopy(data)
    snapshot_data["snapshot_timestamp"] = datetime.now(timezone.utc).isoformat()

    if snapshot_dir is None:
        snapshot_dir = progress_path.parent / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    snapshot_path = snapshot_dir / f"progress_snapshot_{ts}.json"
    write_json(snapshot_data, snapshot_path)
    return snapshot_path


def get_summary(progress_path: Path) -> dict:
    """Get a human-readable summary of current progress."""
    data = load_progress(progress_path)
    return {
        "phase": data.get("current_phase", "unknown"),
        "wave": data.get("current_wave"),
        "tier": data.get("current_tier", 0),
        "last_completed": data.get("last_completed_feature"),
        "in_progress": data.get("in_progress_features", []),
        "issues": data.get("known_issues", []),
        "environment": data.get("environment_state", "unknown"),
        "next_steps": data.get("next_steps"),
        "total_sessions": len(data.get("session_history", [])),
    }


def main() -> None:
    # Parent parser for shared --path argument (inherited by all subcommands)
    path_parent = argparse.ArgumentParser(add_help=False)
    path_parent.add_argument("--path", type=Path, default=None, help="Path to claude-progress.json")

    parser = argparse.ArgumentParser(description="AAH progress manager")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("read", help="Read current progress", parents=[path_parent])
    sub.add_parser("summary", help="Get progress summary", parents=[path_parent])

    init_p = sub.add_parser("init", help="Create new progress file", parents=[path_parent])
    init_p.add_argument("--phase", default="init")

    update_p = sub.add_parser("update", help="Update progress fields", parents=[path_parent])
    update_p.add_argument("--phase", type=str)
    update_p.add_argument("--wave", type=int)
    update_p.add_argument("--tier", type=int)
    update_p.add_argument("--completed-feature", type=str)
    update_p.add_argument("--in-progress", type=str, nargs="*")
    update_p.add_argument("--issue", type=str)
    update_p.add_argument("--remove-issue", type=str)
    update_p.add_argument("--env-state", type=str)
    update_p.add_argument("--next-steps", type=str)

    snap_p = sub.add_parser("snapshot", help="Take a progress snapshot", parents=[path_parent])
    snap_p.add_argument("--snapshot-dir", type=Path, default=None)

    session_p = sub.add_parser("add-session", help="Add session summary", parents=[path_parent])
    session_p.add_argument("summary", type=str)
    session_p.add_argument("--features", type=str, nargs="*")
    session_p.add_argument("--allow-duplicate", action="store_true",
                           help="Append even if it repeats the last entry verbatim")

    args = parser.parse_args()
    progress_path = args.path

    if args.command == "init":
        if progress_path is None:
            progress_path = Path.cwd() / ".aah" / PROGRESS_FILENAME
        data = get_default_progress()
        data["current_phase"] = args.phase
        save_progress(data, progress_path)
        json.dump(data, sys.stdout, indent=2)
        print()
        sys.exit(0)

    # Find progress file for other commands
    if progress_path is None:
        progress_path = find_progress()
        if progress_path is None:
            print("Error: claude-progress.json not found", file=sys.stderr)
            sys.exit(1)

    if args.command == "read":
        data = load_progress(progress_path)
        json.dump(data, sys.stdout, indent=2)
        print()

    elif args.command == "summary":
        summary = get_summary(progress_path)
        json.dump(summary, sys.stdout, indent=2)
        print()

    elif args.command == "update":
        update_progress(
            progress_path,
            phase=args.phase,
            wave=args.wave,
            tier=args.tier,
            completed_feature=args.completed_feature,
            in_progress=args.in_progress,
            issue=args.issue,
            remove_issue=args.remove_issue,
            env_state=args.env_state,
            next_steps=args.next_steps,
        )
        print("Progress updated", file=sys.stderr)

    elif args.command == "snapshot":
        snap_path = snapshot(progress_path, args.snapshot_dir)
        print(f"Snapshot saved to {snap_path}", file=sys.stderr)

    elif args.command == "add-session":
        before = len(load_progress(progress_path).get("session_history") or [])
        data = add_session_entry(
            progress_path, args.summary, args.features,
            allow_duplicate=args.allow_duplicate,
        )
        added = len(data.get("session_history") or []) > before
        print("Session entry added" if added
              else "Session entry skipped (duplicate of last)", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
