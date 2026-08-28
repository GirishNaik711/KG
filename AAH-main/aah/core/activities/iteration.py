#!/usr/bin/env python3
"""Iteration lifecycle management for the RAPIDS delivery framework.

Tracks iterations in .aah/iteration-history.yaml, produces re-entry recaps
when returning to a project, and synthesizes prior-iteration intelligence for
use in subsequent iterations.

Usage:
    aah run core.activities.iteration history --aah-path /path/.aah
    aah run core.activities.iteration start --aah-path /path/.aah --scenario S4 --scope "Add triage" --project-type ai-infra-platforms
    aah run core.activities.iteration complete --aah-path /path/.aah --features F001,F002 --decisions "ADR-001" --artifacts "research/foo.md"
    aah run core.activities.iteration recap --aah-path /path/.aah
    aah run core.activities.iteration synthesize --aah-path /path/.aah
"""

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from aah.core.common.io_utils import read_yaml, write_yaml

_HISTORY_FILENAME = "iteration-history.yaml"
def _empty_history() -> dict:
    """Return a fresh empty history dict (avoids mutable default sharing)."""
    return {"iterations": []}


# ---------------------------------------------------------------------------
# Core data access
# ---------------------------------------------------------------------------


def load_iteration_history(rapids_path: Path) -> dict:
    """Load iteration-history.yaml. Return empty structure if it doesn't exist."""
    history_file = rapids_path / _HISTORY_FILENAME
    if not history_file.exists():
        return _empty_history()
    data = read_yaml(history_file)
    if not isinstance(data, dict) or "iterations" not in data:
        return _empty_history()
    return data


def _save_iteration_history(rapids_path: Path, history: dict) -> None:
    """Persist iteration history to disk."""
    write_yaml(history, rapids_path / _HISTORY_FILENAME)


def get_current_iteration(rapids_path: Path) -> dict | None:
    """Return the in_progress iteration, or None if none exists."""
    history = load_iteration_history(rapids_path)
    for iteration in history.get("iterations", []):
        if iteration.get("status") == "in_progress":
            return iteration
    return None


# ---------------------------------------------------------------------------
# Lifecycle operations
# ---------------------------------------------------------------------------


def start_iteration(
    rapids_path: Path,
    scenario: str,
    scope: str,
    project_types: list[str],
) -> dict:
    """Start a new iteration.

    Auto-computes the next iteration number and writes to iteration-history.yaml.
    Returns the new iteration record.

    Raises ValueError if an iteration is already in_progress.
    """
    history = load_iteration_history(rapids_path)
    iterations = history.get("iterations", [])

    # Guard: only one iteration may be in_progress at a time
    for it in iterations:
        if it.get("status") == "in_progress":
            raise ValueError(
                f"Iteration {it['number']} is already in progress. "
                "Complete it before starting a new one."
            )

    next_number = max((it.get("number", 0) for it in iterations), default=0) + 1

    new_iteration: dict = {
        "number": next_number,
        "started": date.today().isoformat(),
        "completed": None,
        "scenario": scenario,
        "scope": scope,
        "project_types": list(project_types),
        "features_built": [],
        "key_decisions": [],
        "artifacts_produced": [],
        "status": "in_progress",
    }

    history.setdefault("iterations", []).append(new_iteration)
    _save_iteration_history(rapids_path, history)
    return new_iteration


def complete_iteration(
    rapids_path: Path,
    features_built: list[str],
    key_decisions: list[str],
    artifacts_produced: list[str],
) -> dict:
    """Mark the current in_progress iteration as completed.

    Updates the record with completion date and provided metadata.
    Returns the completed record.

    Raises ValueError if no iteration is in_progress.
    """
    history = load_iteration_history(rapids_path)
    iterations = history.get("iterations", [])

    for iteration in iterations:
        if iteration.get("status") == "in_progress":
            iteration["completed"] = date.today().isoformat()
            iteration["status"] = "completed"
            iteration["features_built"] = list(features_built)
            iteration["key_decisions"] = list(key_decisions)
            iteration["artifacts_produced"] = list(artifacts_produced)
            _save_iteration_history(rapids_path, history)
            return iteration

    raise ValueError("No iteration is currently in_progress.")


# ---------------------------------------------------------------------------
# Re-entry recap
# ---------------------------------------------------------------------------


def _days_since(date_str: str | None) -> str:
    """Return a human-readable string for the number of days since a date."""
    if not date_str:
        return "unknown"
    try:
        past = date.fromisoformat(date_str)
        delta = (date.today() - past).days
        if delta == 0:
            return "today"
        if delta == 1:
            return "1 day"
        return f"{delta} days"
    except (ValueError, TypeError):
        return "unknown"


def build_reentry_recap(rapids_path: Path) -> dict:
    """Build a re-entry recap for a returning user.

    Summarises completed iterations, highlights the most recent one,
    computes time elapsed since it finished, and lists available artifacts.
    """
    history = load_iteration_history(rapids_path)
    iterations = history.get("iterations", [])
    completed = [it for it in iterations if it.get("status") == "completed"]

    if not completed:
        return {
            "total_iterations": 0,
            "last_iteration": None,
            "time_since_last": None,
            "artifacts_available": [],
            "summary": "No completed iterations found.",
        }

    last = completed[-1]
    time_since = _days_since(last.get("completed"))

    # Collect all artifacts across completed iterations
    artifacts_available: list[str] = []
    for it in completed:
        for artifact in it.get("artifacts_produced", []):
            if artifact not in artifacts_available:
                artifacts_available.append(artifact)

    # Build human-readable summary
    n_features = len(last.get("features_built", []))
    decisions = last.get("key_decisions", [])
    decision_preview = "; ".join(decisions[:2])

    time_phrase = time_since if time_since in ("unknown", "today") else f"{time_since} ago"
    summary_parts = [
        f"Iteration {last['number']} completed on {last.get('completed', 'unknown')} "
        f"({time_phrase}).",
        f"Built {n_features} feature{'s' if n_features != 1 else ''}.",
    ]
    if decision_preview:
        summary_parts.append(f"Key decisions: {decision_preview}.")

    return {
        "total_iterations": len(completed),
        "last_iteration": {
            "number": last.get("number"),
            "completed": last.get("completed"),
            "scenario": last.get("scenario"),
            "scope": last.get("scope"),
            "features_built": last.get("features_built", []),
            "key_decisions": last.get("key_decisions", []),
        },
        "time_since_last": time_since,
        "artifacts_available": artifacts_available,
        "summary": " ".join(summary_parts),
    }


# ---------------------------------------------------------------------------
# Prior intelligence synthesis
# ---------------------------------------------------------------------------


def _extract_artifact_metadata(file_path: Path) -> dict:
    """Extract title and section headings from a markdown artifact."""
    title = ""
    sections: list[str] = []

    try:
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []

    for i, line in enumerate(lines):
        if i < 3 and not title and line.strip():
            title = line.strip().lstrip("#").strip()
        if line.startswith("##"):
            sections.append(line.strip())

    return {
        "file": file_path.name,
        "title": title,
        "sections": sections,
    }


def synthesize_prior_intelligence(rapids_path: Path) -> dict:
    """Produce a synthesis of all prior iteration artifacts.

    Scans .aah/research/ and .aah/analysis/ for markdown files.
    For ADR files in .aah/analysis/decisions/, they are also listed
    separately under 'decisions'.

    Returns a structured dict suitable for injecting into LLM context.
    """
    research_dir = rapids_path / "research"
    analysis_dir = rapids_path / "analysis"
    decisions_dir = analysis_dir / "decisions"

    def _collect_md(directory: Path) -> list[dict]:
        if not directory.exists():
            return []
        files = sorted(directory.glob("*.md"))
        return [_extract_artifact_metadata(f) for f in files]

    research_artifacts = _collect_md(research_dir)
    # analysis artifacts excluding the decisions subdirectory
    analysis_artifacts: list[dict] = []
    if analysis_dir.exists():
        for f in sorted(analysis_dir.glob("*.md")):
            analysis_artifacts.append(_extract_artifact_metadata(f))

    decisions = _collect_md(decisions_dir)

    history = load_iteration_history(rapids_path)
    iteration_count = len(
        [it for it in history.get("iterations", []) if it.get("status") == "completed"]
    )

    n_research = len(research_artifacts)
    n_analysis = len(analysis_artifacts)
    n_decisions = len(decisions)

    summary = (
        f"Prior iterations produced {n_research} research artifact{'s' if n_research != 1 else ''}, "
        f"{n_analysis} analysis artifact{'s' if n_analysis != 1 else ''}, "
        f"and {n_decisions} architecture decision{'s' if n_decisions != 1 else ''}."
    )

    return {
        "iteration_count": iteration_count,
        "research_artifacts": research_artifacts,
        "analysis_artifacts": analysis_artifacts,
        "decisions": decisions,
        "synthesis_summary": summary,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="RAPIDS Iteration Lifecycle Manager")
    sub = parser.add_subparsers(dest="command", required=True)

    def _rp(p: argparse.ArgumentParser) -> None:
        p.add_argument("--aah-path", "--rapids-path", dest="aah_path",
                       type=Path, required=True,
                       help="Path to the .aah directory "
                            "(--rapids-path accepted as a deprecated alias)")

    _rp(sub.add_parser("history", help="Show full iteration history"))

    start_p = sub.add_parser("start", help="Start a new iteration")
    _rp(start_p)
    start_p.add_argument("--scenario", type=str, required=True, help="Scenario ID, e.g. S4")
    start_p.add_argument("--scope", type=str, required=True, help="Scope description")
    start_p.add_argument("--project-type", type=str, nargs="+", required=True,
                         help="Project type identifier(s), e.g. ai-infra-platforms")

    complete_p = sub.add_parser("complete", help="Mark the current iteration as completed")
    _rp(complete_p)
    complete_p.add_argument("--features", type=str, default="",
                            help="Comma-separated feature IDs built")
    complete_p.add_argument("--decisions", type=str, default="",
                            help="Comma-separated key decision strings")
    complete_p.add_argument("--artifacts", type=str, default="",
                            help="Comma-separated artifact paths produced")

    _rp(sub.add_parser("recap", help="Show re-entry recap for a returning user"))
    _rp(sub.add_parser("synthesize", help="Synthesize prior intelligence from artifacts"))

    args = parser.parse_args()

    if args.command == "history":
        result = load_iteration_history(args.aah_path)

    elif args.command == "start":
        result = start_iteration(
            rapids_path=args.aah_path,
            scenario=args.scenario,
            scope=args.scope,
            project_types=args.project_type,
        )

    elif args.command == "complete":
        features = [f.strip() for f in args.features.split(",") if f.strip()]
        decisions = [d.strip() for d in args.decisions.split(",") if d.strip()]
        artifacts = [a.strip() for a in args.artifacts.split(",") if a.strip()]
        result = complete_iteration(
            rapids_path=args.aah_path,
            features_built=features,
            key_decisions=decisions,
            artifacts_produced=artifacts,
        )

    elif args.command == "recap":
        result = build_reentry_recap(args.aah_path)

    elif args.command == "synthesize":
        result = synthesize_prior_intelligence(args.aah_path)

    else:
        parser.print_help()
        sys.exit(1)

    json.dump(result, sys.stdout, indent=2)
    print()
    sys.exit(0)


if __name__ == "__main__":
    main()
