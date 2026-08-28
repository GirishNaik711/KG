#!/usr/bin/env python3
"""
Stop hook: generate end-of-session summary and append to claude-progress.json.

Reads git log since last session, test results, and current progress
to generate a concise session summary.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.feature_list import get_progress_summary, load_feature_list
from aah.core.common.git_utils import get_log
from aah.core.common.io_utils import read_json
from aah.core.common.progress import add_session_entry, load_progress


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp into an aware datetime, or None.

    Naive values are assumed UTC so comparisons never raise on mixed awareness.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def generate_session_summary(project_path: Path) -> dict:
    """Generate a summary of what happened in this session."""
    aah_path = project_path / ".aah"

    progress_path = aah_path / "claude-progress.json"
    try:
        progress = load_progress(progress_path if progress_path.exists() else None)
    except Exception as e:
        # If progress file is corrupted, log warning and use defaults
        print(f"Warning: Could not load progress file: {e}", file=sys.stderr)
        print("Continuing with default progress state...", file=sys.stderr)
        from aah.core.common.progress import get_default_progress
        progress = get_default_progress()

    # Get commits since last session
    last_session_ts = None
    history = progress.get("session_history", [])
    if history:
        last_session_ts = history[-1].get("timestamp")
    cutoff = _parse_ts(last_session_ts)

    recent_commits = get_log(cwd=project_path, count=50)

    # Filter commits to those since last session
    session_commits = []
    for commit in recent_commits:
        commit_ts = _parse_ts(commit.get("date"))
        if cutoff and commit_ts and commit_ts < cutoff:
            break
        session_commits.append(commit)

    # Get current feature progress
    fl_path = aah_path / "feature-list.json"
    feature_summary = {}
    if fl_path.exists():
        fl_data = load_feature_list(fl_path)
        feature_summary = get_progress_summary(fl_data)

    # Check test results
    test_results_dir = aah_path / "build" / "test-results"
    recent_test_results = []
    if test_results_dir.is_dir():
        for result_file in sorted(test_results_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:10]:
            try:
                result = read_json(result_file)
                result_ts = _parse_ts(result.get("timestamp"))
                if cutoff is None or (result_ts and result_ts >= cutoff):
                    recent_test_results.append({
                        "feature": result.get("feature_id", result_file.stem),
                        "passed": result.get("passed", False),
                    })
            except Exception:
                pass

    # Build summary text
    parts = []
    if session_commits:
        parts.append(f"{len(session_commits)} commit(s) made")

    features_completed = []
    for tr in recent_test_results:
        if tr["passed"]:
            features_completed.append(tr["feature"])

    # Cross-reference feature-list.json (authoritative pass/fail record)
    if fl_path.exists():
        currently_passing = {
            f["id"] for f in fl_data.get("features", []) if f.get("passes", False)
        }
        for tr in recent_test_results:
            if tr["feature"] in currently_passing and tr["feature"] not in features_completed:
                features_completed.append(tr["feature"])

    if features_completed:
        parts.append(f"Features completed: {', '.join(features_completed)}")

    if feature_summary:
        parts.append(f"Progress: {feature_summary.get('passing', 0)}/{feature_summary.get('total', 0)} features passing")

    issues = progress.get("known_issues", [])
    if issues:
        parts.append(f"Open issues: {len(issues)}")

    summary_text = ". ".join(parts) if parts else "Session with no tracked changes"

    # Extract decisions and patterns from commit messages
    decisions_made = []
    patterns_discovered = []
    for c in session_commits:
        # get_log emits "subject", not "message" — reading "message" always
        # returned "" and left both lists permanently empty.
        full_msg = c.get("subject", "")
        msg = full_msg.lower()
        if any(kw in msg for kw in ["decision:", "chose ", "switched to", "adr"]):
            decisions_made.append(full_msg.split("\n")[0].strip())
        if any(kw in msg for kw in ["pattern:", "convention:", "discovered that"]):
            patterns_discovered.append(full_msg.split("\n")[0].strip())

    # Detect blockers resolved: compare known_issues from start vs now
    blockers_resolved = []
    if history:
        prev_issues = set()
        for h in history[-3:]:
            for issue in h.get("open_issues", []):
                prev_issues.add(issue)
        current_issues = set(issues)
        resolved = prev_issues - current_issues
        blockers_resolved = list(resolved)[:5]

    summary = {
        "summary": summary_text,
        "commits": len(session_commits),
        "features_completed": features_completed,
        "feature_progress": feature_summary,
        "test_results": recent_test_results,
        "decisions_made": decisions_made,
        "patterns_discovered": patterns_discovered,
        "blockers_resolved": blockers_resolved,
        "open_issues": issues[:5],
    }

    return summary


def _should_record(summary: dict, progress_path: Path) -> bool:
    """True if this turn is worth appending to session_history.

    A cheap pre-filter: it skips the obvious no-content, no-transition turns
    before touching the journal. It is not the last word — ``add_session_entry``
    drops anything that would repeat the previous entry verbatim, so a content
    signal here cannot resurrect a duplicate.
    """
    if summary.get("commits") or summary.get("features_completed"):
        return True
    if summary.get("blockers_resolved") or summary.get("decisions_made"):
        return True
    if summary.get("patterns_discovered"):
        return True
    try:
        history = load_progress(progress_path).get("session_history") or []
    except Exception:
        return True  # unreadable progress — fall through and record
    if not history:
        return True
    return summary.get("summary") != history[-1].get("summary")


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    from aah.core.common.config import resolve_project_path
    cwd = hook_input.get("cwd")
    project_path = resolve_project_path(Path(cwd) if cwd else None)

    if project_path is None or not (project_path / ".aah").is_dir():
        # Not a AAH project — emit empty summary, do not block the hook
        json.dump({"summary": "", "features_completed": []}, sys.stdout, indent=2)
        print()
        sys.exit(0)

    summary = generate_session_summary(project_path)

    # Append to progress journal — only when the turn actually produced something.
    progress_path = project_path / ".aah" / "claude-progress.json"
    if progress_path.exists() and _should_record(summary, progress_path):
        add_session_entry(
            progress_path,
            summary["summary"],
            summary["features_completed"],
        )

    json.dump(summary, sys.stdout, indent=2)
    print()
    sys.exit(0)


if __name__ == "__main__":
    main()
