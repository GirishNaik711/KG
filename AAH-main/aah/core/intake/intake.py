#!/usr/bin/env python3
"""Core intake data model and persistence for .aah/intake.json."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.io_utils import read_json, write_json


INTAKE_FILENAME = "intake.json"
INTAKE_VERSION = "1.0"


def resolve_intake_path(raw: Path | None) -> Path | None:
    """Normalize a user-supplied path to an intake.json file path.

    Accepts a directory (appends intake.json), a file path, or None (uses find_intake).
    """
    if raw is None:
        return find_intake()
    if raw.is_dir() or raw.suffix != ".json":
        return raw / INTAKE_FILENAME
    return raw


def get_default_intake() -> dict:
    """Return a blank intake structure."""
    return {
        "version": INTAKE_VERSION,
        "status": "not_started",
        "problem_statement": None,
        "raw_context": None,
        "project_type": None,
        "rounds": [],
        "complexity_assessment": None,
        "phase_plan": None,
        # industry_domain is populated by aah.core.domain_briefs.classifier
        # during /rapids-research. Null until a domain is chosen or
        # explicitly declined.
        "industry_domain": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def find_intake(start_dir: Path | None = None) -> Path | None:
    """Find intake.json. If start_dir given, walk up. Otherwise use config."""
    if start_dir:
        for directory in [start_dir, *start_dir.parents]:
            candidate = directory / ".aah" / INTAKE_FILENAME
            if candidate.exists():
                return candidate
        return None
    from aah.core.common.config import get_active_project_rapids_path
    rapids_path = get_active_project_rapids_path()
    if rapids_path:
        candidate = rapids_path / INTAKE_FILENAME
        if candidate.exists():
            return candidate
    return None


def load_intake(path: Path | None = None) -> dict:
    """Load intake.json or return default."""
    if path is None:
        path = find_intake()
    if path is None or not path.exists():
        return get_default_intake()
    try:
        return read_json(path)
    except (json.JSONDecodeError, OSError):
        return get_default_intake()


def save_intake(data: dict, path: Path) -> None:
    """Save intake.json with updated timestamp."""
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_json(data, path)


def add_qa_round(intake: dict, phase: str, questions_and_answers: list[dict]) -> dict:
    """
    Append a Q&A round to the intake.

    questions_and_answers: list of {"question": str, "answer": str, "header": str (optional)}
    """
    round_entry = {
        "phase": phase,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "questions": questions_and_answers,
    }
    intake["rounds"].append(round_entry)

    # Update status
    if intake["status"] == "not_started":
        intake["status"] = "in_progress"

    return intake


def set_problem_statement(intake: dict, statement: str) -> dict:
    """Set the problem statement."""
    intake["problem_statement"] = statement
    if intake["status"] == "not_started":
        intake["status"] = "in_progress"
    return intake


def set_raw_context(intake: dict, context: str) -> dict:
    """Preserve the full user-provided context verbatim."""
    intake["raw_context"] = context
    return intake


def set_project_type(intake: dict, project_type: str) -> dict:
    """Set the project type."""
    valid_types = ["new_system", "enhancement", "bug_fix", "migration"]
    if project_type not in valid_types:
        raise ValueError(f"Invalid project type: {project_type}. Must be one of {valid_types}")
    intake["project_type"] = project_type
    return intake


def set_complexity_assessment(intake: dict, assessment: dict) -> dict:
    """Set the complexity assessment result."""
    intake["complexity_assessment"] = assessment
    return intake


def set_phase_plan(intake: dict, phase_plan: dict) -> dict:
    """Set the phase plan derived from complexity."""
    intake["phase_plan"] = phase_plan
    return intake


def mark_complete(intake: dict) -> dict:
    """Mark intake as complete."""
    intake["status"] = "complete"
    return intake


def get_all_answers(intake: dict) -> dict[str, str]:
    """Flatten all Q&A rounds into a single dict of question->answer."""
    answers = {}
    for round_entry in intake.get("rounds", []):
        for qa in round_entry.get("questions", []):
            question = qa.get("question")
            answer = qa.get("answer")
            if question and answer:
                answers[question] = answer
    return answers


def get_answers_for_phase(intake: dict, phase: str) -> list[dict]:
    """Get all Q&A pairs for a specific phase."""
    results = []
    for round_entry in intake.get("rounds", []):
        if round_entry.get("phase") == phase:
            results.extend(round_entry.get("questions", []))
    return results


def get_answered_topics(intake: dict) -> set[str]:
    """Get the set of topic headers that have been answered."""
    topics = set()
    for round_entry in intake.get("rounds", []):
        for qa in round_entry.get("questions", []):
            header = qa.get("header")
            if header:
                topics.add(header)
    return topics


def is_complete(intake: dict) -> bool:
    """Check if minimum intake is satisfied for the complexity tier."""
    if intake.get("status") == "complete":
        return True

    # Must have problem statement
    if not intake.get("problem_statement"):
        return False

    # Must have project type
    if not intake.get("project_type"):
        return False

    # Must have complexity assessment
    if not intake.get("complexity_assessment"):
        return False

    tier = intake["complexity_assessment"].get("tier", "moderate")

    # Minimum rounds by tier
    min_rounds = {"trivial": 1, "moderate": 1, "significant": 2, "complex": 2}
    actual_rounds = len(intake.get("rounds", []))
    if actual_rounds < min_rounds.get(tier, 1):
        return False

    return True


def get_intake_summary(intake: dict, max_entries: int = 0) -> str:
    """Generate a human-readable summary for context injection.

    Args:
        intake: The intake data dict.
        max_entries: Maximum Q&A entries to include. 0 means unlimited.
    """
    lines = []

    ps = intake.get("problem_statement")
    if ps:
        lines.append(f"Problem: {ps}")

    rc = intake.get("raw_context")
    if rc:
        lines.append(f"\nOriginal context provided by user:\n{rc}")

    pt = intake.get("project_type")
    if pt:
        lines.append(f"Type: {pt}")

    ca = intake.get("complexity_assessment")
    if ca:
        lines.append(f"Complexity: {ca.get('tier', 'unknown')} — {ca.get('reasoning', '')}")

    pp = intake.get("phase_plan")
    if pp:
        active = [p for p, cfg in pp.items() if cfg.get("required")]
        lines.append(f"Phases: {', '.join(active)}")

    # Include Q&A grouped by phase
    rounds = intake.get("rounds", [])
    if rounds:
        lines.append("\nKey decisions from intake:")
        # Group by phase for readability
        phase_qa: dict[str, list[tuple[str, str]]] = {}
        for round_entry in rounds:
            phase = round_entry.get("phase", "general")
            if phase not in phase_qa:
                phase_qa[phase] = []
            for qa in round_entry.get("questions", []):
                question = qa.get("question")
                answer = qa.get("answer")
                if question and answer:
                    phase_qa[phase].append((question, answer))

        total_entries = 0
        for phase, qa_pairs in phase_qa.items():
            if max_entries and total_entries >= max_entries:
                break
            lines.append(f"  [{phase}]")
            for q, a in qa_pairs:
                if max_entries and total_entries >= max_entries:
                    remaining = sum(len(v) for v in phase_qa.values()) - total_entries
                    lines.append(f"  ... and {remaining} more entries")
                    break
                lines.append(f"  Q: {q}")
                lines.append(f"  A: {a}")
                total_entries += 1

    return "\n".join(lines)


def main() -> None:
    path_parent = argparse.ArgumentParser(add_help=False)
    path_parent.add_argument("--path", "--intake-path", type=Path, default=None,
                             help="Path to intake.json or its parent directory")

    parser = argparse.ArgumentParser(description="RAPIDS intake manager")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("read", parents=[path_parent])
    sub.add_parser("summary", parents=[path_parent])
    sub.add_parser("status", parents=[path_parent])

    init_p = sub.add_parser("init", parents=[path_parent])
    init_p.add_argument("--problem", type=str, default=None)
    init_p.add_argument("--type", dest="project_type", type=str, default=None)
    init_p.add_argument("--raw-context", type=str, default=None,
                        help="Full user-provided context to preserve verbatim")
    init_p.add_argument("--raw-context-file", type=Path, default=None,
                        help="File containing raw context (for large text)")

    add_p = sub.add_parser("add-round", parents=[path_parent])
    add_p.add_argument("--phase", type=str, required=True)
    add_p.add_argument("--qa-json", type=str, required=True, help="JSON array of {question, answer}")

    sub.add_parser("complete", parents=[path_parent])

    args = parser.parse_args()

    intake_path = resolve_intake_path(args.path)

    if args.command == "init":
        if intake_path is None:
            from aah.core.common.config import get_active_project_rapids_path
            rapids_path = get_active_project_rapids_path()
            if rapids_path:
                intake_path = rapids_path / INTAKE_FILENAME
            else:
                intake_path = Path.cwd() / ".aah" / INTAKE_FILENAME
        intake = get_default_intake()
        if args.problem:
            set_problem_statement(intake, args.problem)
        if args.project_type:
            set_project_type(intake, args.project_type)
        raw_ctx = args.raw_context
        if not raw_ctx and args.raw_context_file:
            raw_ctx = args.raw_context_file.read_text(encoding='utf-8')
        if raw_ctx:
            set_raw_context(intake, raw_ctx)
        save_intake(intake, intake_path)
        json.dump(intake, sys.stdout, indent=2)
        print()
        sys.exit(0)

    # Auto-resolve intake path from config if not found
    if intake_path is None:
        from aah.core.common.config import get_active_project_rapids_path
        rapids_path = get_active_project_rapids_path()
        if rapids_path:
            intake_path = rapids_path / INTAKE_FILENAME
        else:
            print("Error: intake.json not found and no active project configured", file=sys.stderr)
            sys.exit(1)

    if args.command == "read":
        intake = load_intake(intake_path)
        json.dump(intake, sys.stdout, indent=2)
        print()

    elif args.command == "summary":
        intake = load_intake(intake_path)
        print(get_intake_summary(intake))

    elif args.command == "status":
        intake = load_intake(intake_path)
        complete = is_complete(intake)
        json.dump({
            "status": intake.get("status"),
            "is_complete": complete,
            "rounds": len(intake.get("rounds", [])),
            "has_problem": intake.get("problem_statement") is not None,
            "has_type": intake.get("project_type") is not None,
            "has_complexity": intake.get("complexity_assessment") is not None,
        }, sys.stdout, indent=2)
        print()

    elif args.command == "add-round":
        intake = load_intake(intake_path)
        qa = json.loads(args.qa_json)
        add_qa_round(intake, args.phase, qa)
        save_intake(intake, intake_path)
        print(f"Added Q&A round for phase '{args.phase}'", file=sys.stderr)

    elif args.command == "complete":
        intake = load_intake(intake_path)
        mark_complete(intake)
        save_intake(intake, intake_path)
        print("Intake marked complete", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
