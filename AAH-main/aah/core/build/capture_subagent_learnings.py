#!/usr/bin/env python3
"""
SubagentStop hook: capture learnings from aah-feature-implementer subagents.

Reads the subagent's recent commits and extracts patterns, decisions,
and conventions discovered during implementation. Appends findings
to .aah/build/subagent-learnings.json so the next subagent benefits.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.feature_utils import append_section, find_feature_file

from aah.core.common.git_utils import get_log
from aah.core.common.io_utils import read_json, read_yaml, write_json, write_yaml


LEARNINGS_FILENAME = "subagent-learnings.json"


def capture_learnings(project_path: Path, agent_name: str = "", cwd: str | None = None) -> dict:
    """
    Capture learnings from a subagent's recent work.

    Returns a dict with extracted learnings.
    """
    aah_path = project_path / ".aah" / "build"
    aah_path.mkdir(parents=True, exist_ok=True)

    # Get recent commits (likely from the subagent)
    try:
        recent = get_log(cwd=project_path, count=10)
    except Exception:
        recent = []

    # Extract patterns, decisions, conventions from commit messages
    patterns = []
    decisions = []
    conventions = []

    for commit in recent:
        msg = commit.get("subject", commit.get("message", ""))
        msg_lower = msg.lower()
        first_line = msg.split("\n")[0].strip()

        if any(msg_lower.startswith(kw) for kw in ["pattern:", "pattern(", "convention:", "convention("]) or any(kw in msg_lower for kw in ["discovered"]):
            patterns.append(first_line)
        if any(msg_lower.startswith(kw) for kw in ["decision:", "decision("]) or any(kw in msg_lower for kw in ["chose ", "switched to"]):
            decisions.append(first_line)
        if any(msg_lower.startswith(kw) for kw in ["fix:", "fix(", "workaround:", "workaround(", "note:", "note("]):
            conventions.append(first_line)

    learning_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent_name": agent_name,
        "commits_analyzed": len(recent),
        "patterns": patterns,
        "decisions": decisions,
        "conventions": conventions,
    }

    # Skip writing if no learnings were extracted
    if not patterns and not decisions and not conventions:
        return learning_entry

    # Load existing learnings and append
    learnings_path = aah_path / LEARNINGS_FILENAME
    if learnings_path.exists():
        try:
            data = read_json(learnings_path)
        except Exception:
            data = {"entries": []}
    else:
        data = {"entries": []}

    data["entries"].append(learning_entry)
    # Keep last 20 entries
    data["entries"] = data["entries"][-20:]
    data["last_updated"] = datetime.now(timezone.utc).isoformat()

    write_json(data, learnings_path)

    # Also write learnings into the feature YAML's knowledge_used.subagent_learnings
    # Derive feature_id from the worktree's git branch (e.g. feature/F005-fetch-prices → F005).
    # This is more reliable than agent_name because Claude Code's SubagentStop hook payload
    # does not include the agent's name field.
    import re
    import subprocess

    feature_id = ""
    worktree_cwd = cwd or str(project_path)
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree_cwd, text=True, stderr=subprocess.DEVNULL,
        ).strip()
        m = re.search(r'\b(F\d+)\b', branch, re.IGNORECASE)
        if m:
            feature_id = m.group(1).upper()
    except Exception:
        pass

    if feature_id and (patterns or decisions or conventions):
        features_dir = project_path / ".aah" / "plan" / "features"
        feature_file_path = find_feature_file(features_dir, feature_id)
        if feature_file_path:
            try:
                section_lines = []
                if patterns:
                    section_lines.append("- Patterns:")
                    for p in patterns:
                        section_lines.append(f"  - {p}")
                if decisions:
                    section_lines.append("- Decisions:")
                    for d in decisions:
                        section_lines.append(f"  - {d}")
                if conventions:
                    section_lines.append("- Conventions:")
                    for c in conventions:
                        section_lines.append(f"  - {c}")
                append_section(feature_file_path, "Subagent Learnings", section_lines)
            except Exception:
                pass  # Non-fatal — global write already succeeded

    return learning_entry


def get_accumulated_learnings(project_path: Path) -> dict:
    """Get all accumulated subagent learnings for context injection."""
    learnings_path = project_path / ".aah" / "build" / LEARNINGS_FILENAME
    if not learnings_path.exists():
        return {"patterns": [], "decisions": [], "conventions": []}

    try:
        data = read_json(learnings_path)
    except Exception:
        return {"patterns": [], "decisions": [], "conventions": []}

    all_patterns = []
    all_decisions = []
    all_conventions = []

    for entry in data.get("entries", []):
        all_patterns.extend(entry.get("patterns", []))
        all_decisions.extend(entry.get("decisions", []))
        all_conventions.extend(entry.get("conventions", []))

    return {
        "patterns": list(dict.fromkeys(all_patterns))[:15],
        "decisions": list(dict.fromkeys(all_decisions))[:15],
        "conventions": list(dict.fromkeys(all_conventions))[:15],
    }


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    from aah.core.common.config import resolve_project_path
    cwd = hook_input.get("cwd")
    project_path = resolve_project_path(Path(cwd) if cwd else None)

    if project_path is None or not (project_path / ".aah").is_dir():
        sys.exit(0)

    agent_name = hook_input.get("agent_name", "")
    learning = capture_learnings(project_path, agent_name, cwd)

    json.dump(learning, sys.stdout, indent=2)
    print()
    sys.exit(0)


if __name__ == "__main__":
    main()
