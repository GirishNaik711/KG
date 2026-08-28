#!/usr/bin/env python3
"""
Iteration lifecycle manager for multi-sprint AAH projects.

iter-N/ directories are ARCHIVES ONLY — created when archiving a completed
iteration, never pre-created. Work always happens in the active phase
directories (see ITERATION_PHASE_DIRS below). On rollover each phase dir is
copied into iter-N/ then emptied. Shared/cumulative state
(codebase-intel/, brownfield/, manifest.yaml, claude-progress.json, and the
project-root knowledge/ folder) stays in place across iterations;
feature-list.json is cumulative and only snapshotted.
"""

import argparse
import json
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from aah.core.common.io_utils import ensure_dir, read_json, read_yaml, write_json, write_yaml
from aah.core.common.manifest import load_manifest, save_manifest


ITERATIONS_DIR = "iterations"

# Phase dirs that belong to ONE iteration — archived then cleared on rollover.
# Aligned to the iteration-specific subset of scaffold/rapids_dir.py::RAPIDS_DIRS.
ITERATION_PHASE_DIRS = ["discuss", "architecture", "plan", "build", "deploy", "audit"]

# Cumulative/shared files snapshotted into the archive but never cleared.
CUMULATIVE_SNAPSHOT_FILES = ["feature-list.json"]

# Everything else — codebase-intel/, brownfield/, manifest.yaml,
# claude-progress.json, iterations/, and the project-root knowledge/ folder —
# is preserved untouched (never archived, never cleared).


def get_iteration_dir(rapids_path: Path, iteration: int) -> Path:
    """Get the directory for a specific iteration."""
    return rapids_path / ITERATIONS_DIR / f"iter-{iteration}"


def get_current_iteration(rapids_path: Path) -> int:
    """Get the current iteration number from manifest."""
    manifest_path = rapids_path / "manifest.yaml"
    if manifest_path.exists():
        manifest = read_yaml(manifest_path)
        return manifest.get("current_iteration", 1)
    return 1


def get_completed_iterations(rapids_path: Path) -> list[int]:
    """List completed iteration numbers."""
    iterations_dir = rapids_path / ITERATIONS_DIR
    if not iterations_dir.is_dir():
        return []
    completed = []
    for entry in sorted(iterations_dir.iterdir()):
        if entry.is_dir() and entry.name.startswith("iter-"):
            try:
                num = int(entry.name.split("-")[1])
                status_file = entry / "status.json"
                if status_file.exists():
                    status = read_json(status_file)
                    if status.get("status") == "complete":
                        completed.append(num)
            except (ValueError, IndexError):
                continue
    return completed


def detect_needs_new_iteration(rapids_path: Path) -> dict:
    """
    Detect whether the project needs a new iteration.

    Returns:
        {"needs_new": bool, "reason": str, "completed_iterations": int,
         "total_features": int, "passing_features": int}
    """
    manifest_path = rapids_path / "manifest.yaml"
    if not manifest_path.exists():
        return {"needs_new": False, "reason": "No manifest found"}

    manifest = read_yaml(manifest_path)
    phase = manifest.get("current_phase", "init")

    # Check if we're in a terminal phase
    if phase not in ("complete", "deploy"):
        return {
            "needs_new": False,
            "reason": f"Current phase is '{phase}' — iteration still in progress",
            "current_iteration": manifest.get("current_iteration", 1),
        }

    # Check feature completion
    fl_path = rapids_path / "feature-list.json"
    total = 0
    passing = 0
    if fl_path.exists():
        fl = read_json(fl_path)
        features = fl.get("features", [])
        total = len(features)
        passing = len([f for f in features if f.get("passes", False)])

    completed = get_completed_iterations(rapids_path)

    return {
        "needs_new": True,
        "reason": f"Project at phase '{phase}' with {passing}/{total} features passing. Ready for new iteration.",
        "completed_iterations": len(completed),
        "current_iteration": manifest.get("current_iteration", 1),
        "total_features": total,
        "passing_features": passing,
    }


# ---------------------------------------------------------------------------
# Core lifecycle
# ---------------------------------------------------------------------------


def _has_active_work(rapids_path: Path) -> bool:
    """Check if the current iteration has any work in active phase directories.

    True if any ITERATION_PHASE_DIRS entry contains at least one file (recursively).
    """
    for phase in ITERATION_PHASE_DIRS:
        d = rapids_path / phase
        if d.is_dir() and any(f.is_file() for f in d.rglob("*")):
            return True
    return False


def create_new_iteration(rapids_path: Path, problem_statement: str | None = None) -> dict:
    """
    Start a new iteration.

    If the current iteration has work, archives it into iter-N/, generates
    prior-iteration-context.md, clears active directories, and bumps the
    iteration number. If no work exists, stays at iteration 1.
    """
    manifest_path = rapids_path / "manifest.yaml"
    manifest = read_yaml(manifest_path) if manifest_path.exists() else {}
    current_iter = manifest.get("current_iteration", 1)

    if _has_active_work(rapids_path):
        _generate_prior_iteration_context(rapids_path, current_iter)
        _archive_iteration(rapids_path, current_iter)
        _clear_active_state(rapids_path)
        new_iter = current_iter + 1
    else:
        new_iter = 1

    manifest["current_iteration"] = new_iter
    manifest["current_phase"] = "init"
    save_manifest(manifest, manifest_path)

    _sync_to_iteration_history(rapids_path, new_iter, "in_progress", problem_statement)

    return {
        "iteration": new_iter,
        "path": str(rapids_path),
        "prior_iterations": get_completed_iterations(rapids_path),
        "status": "created",
    }


# ---------------------------------------------------------------------------
# Archival helpers
# ---------------------------------------------------------------------------


def _archive_iteration(rapids_path: Path, iteration: int) -> None:
    """Archive current active state into iterations/iter-N/.

    Copies each ITERATION_PHASE_DIRS entry as a full directory tree, then
    snapshots CUMULATIVE_SNAPSHOT_FILES (originals stay — they're cumulative).
    """
    iter_dir = get_iteration_dir(rapids_path, iteration)
    ensure_dir(iter_dir)

    # Each phase dir → iter-N/<phase>/ (whole tree, any file type).
    for phase in ITERATION_PHASE_DIRS:
        src = rapids_path / phase
        if src.is_dir() and any(src.rglob("*")):
            shutil.copytree(src, iter_dir / phase, dirs_exist_ok=True)

    # Cumulative files: snapshot (copy — original stays).
    for fname in CUMULATIVE_SNAPSHOT_FILES:
        src = rapids_path / fname
        if src.exists():
            snapshot_name = f"{src.stem}-snapshot{src.suffix}"
            shutil.copy2(src, iter_dir / snapshot_name)

    # Mark complete
    write_json({
        "iteration": iteration,
        "status": "complete",
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }, iter_dir / "status.json")

    _sync_to_iteration_history(rapids_path, iteration, "completed")


# ---------------------------------------------------------------------------
# Clear active state
# ---------------------------------------------------------------------------


def _clear_active_state(rapids_path: Path) -> None:
    """Empty each ITERATION_PHASE_DIRS entry after it has been archived.

    Removes the contents of every phase dir but keeps the (now empty) dir so the
    scaffolded layout is preserved. Touches nothing else — codebase-intel/,
    brownfield/, manifest.yaml, claude-progress.json, feature-list.json,
    iteration-history.yaml, and the root knowledge/ folder are all preserved.
    """
    for phase in ITERATION_PHASE_DIRS:
        d = rapids_path / phase
        if not d.is_dir():
            continue
        for child in d.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()


# ---------------------------------------------------------------------------
# Prior iteration context (generated for iter-2+ to reference)
# ---------------------------------------------------------------------------


def _generate_prior_iteration_context(rapids_path: Path, completing_iteration: int) -> None:
    """Generate prior-iteration-context.md summarizing all completed iterations.

    Written to the active .aah/ directory BEFORE clearing state.
    Subsequent iterations reference this file — not iter-N/ archives.
    """
    lines = [
        "# Prior Iteration Context",
        "",
        f"Generated when transitioning to iteration {completing_iteration + 1}.",
        "",
    ]

    # Summarize previously archived iterations
    iterations_dir = rapids_path / ITERATIONS_DIR
    if iterations_dir.is_dir():
        for entry in sorted(iterations_dir.iterdir()):
            if entry.is_dir() and entry.name.startswith("iter-"):
                lines.extend(_summarize_archived_iteration(entry))

    # Summarize the current (about-to-be-archived) iteration from active dirs
    lines.extend(_summarize_active_iteration(rapids_path, completing_iteration))

    # Cumulative feature status
    fl_path = rapids_path / "feature-list.json"
    if fl_path.exists():
        fl = read_json(fl_path)
        features = fl.get("features", [])
        passing = [f for f in features if f.get("passes")]
        failing = [f for f in features if not f.get("passes")]
        lines.append("## Cumulative Feature Status")
        lines.append("")
        lines.append(f"Total: {len(features)} | Passing: {len(passing)} | Remaining: {len(failing)}")
        lines.append("")
        if passing:
            lines.append("### Passing Features")
            for f in passing:
                lines.append(f"- **{f['id']}**: {f.get('description', '')}")
            lines.append("")
        if failing:
            lines.append("### Remaining Features")
            for f in failing:
                lines.append(f"- **{f['id']}**: {f.get('description', '')}")
            lines.append("")

    context_path = rapids_path / "prior-iteration-context.md"
    context_path.write_text("\n".join(lines), encoding="utf-8")


def _summarize_active_iteration(rapids_path: Path, iteration: int) -> list[str]:
    """Summarize the current iteration from active directories."""
    lines = [f"## Iteration {iteration}", ""]

    intake_path = rapids_path / "intake.json"
    if intake_path.exists():
        intake = read_json(intake_path)
        ps = intake.get("problem_statement")
        if ps:
            lines.append(f"**Problem Statement:** {ps}")
            lines.append("")

    features_dir = rapids_path / "plan" / "features"
    if features_dir.is_dir():
        yamls = sorted(features_dir.glob("*.yaml"))
        if yamls:
            lines.append(f"**Features Planned:** {len(yamls)}")
            for y in yamls:
                data = read_yaml(y)
                lines.append(f"- {data.get('id', y.stem)}: {data.get('description', '')}")
            lines.append("")

    lines.append("---")
    lines.append("")
    return lines


def _summarize_archived_iteration(iter_dir: Path) -> list[str]:
    """Summarize a previously archived iteration."""
    iter_num = iter_dir.name.replace("iter-", "")
    lines = [f"## Iteration {iter_num}", ""]

    status_path = iter_dir / "status.json"
    if status_path.exists():
        status = read_json(status_path)
        lines.append(f"**Completed:** {status.get('completed_at', 'unknown')}")
        lines.append("")

    intake_path = iter_dir / "intake.json"
    if intake_path.exists():
        intake = read_json(intake_path)
        ps = intake.get("problem_statement")
        if ps:
            lines.append(f"**Problem Statement:** {ps}")
            lines.append("")

    features_dir = iter_dir / "plan" / "features"
    if not features_dir.is_dir():
        features_dir = iter_dir / "features"  # legacy fallback
    if features_dir.is_dir():
        yamls = sorted(features_dir.glob("*.yaml"))
        if yamls:
            lines.append(f"**Features:** {len(yamls)}")
            for y in yamls:
                data = read_yaml(y)
                lines.append(f"- {data.get('id', y.stem)}: {data.get('description', '')}")
            lines.append("")

    fl_snapshot = iter_dir / "feature-list-snapshot.json"
    if fl_snapshot.exists():
        fl = read_json(fl_snapshot)
        features = fl.get("features", [])
        passing = len([f for f in features if f.get("passes")])
        lines.append(f"**Feature Results:** {passing}/{len(features)} passing")
        lines.append("")

    lines.append("---")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Iteration history sync
# ---------------------------------------------------------------------------


def _sync_to_iteration_history(rapids_path: Path, iteration: int, status: str,
                                scope: str | None = None) -> None:
    """Keep iteration-history.yaml in sync with manager lifecycle events."""
    history_file = rapids_path / "iteration-history.yaml"
    if history_file.exists():
        history = read_yaml(history_file)
        if not isinstance(history, dict) or "iterations" not in history:
            history = {"iterations": []}
    else:
        history = {"iterations": []}

    iterations = history["iterations"]

    if status == "completed":
        for it in iterations:
            if it.get("number") == iteration and it.get("status") == "in_progress":
                it["status"] = "completed"
                it["completed"] = date.today().isoformat()
                write_yaml(history, history_file)
                return
        iterations.append({
            "number": iteration,
            "completed": date.today().isoformat(),
            "status": "completed",
            "scope": scope,
        })
    elif status == "in_progress":
        for it in iterations:
            if it.get("number") == iteration:
                return  # already tracked
        iterations.append({
            "number": iteration,
            "started": date.today().isoformat(),
            "status": "in_progress",
            "scope": scope,
        })

    write_yaml(history, history_file)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def get_iteration_summary(rapids_path: Path) -> dict:
    """Get a summary of all iterations."""
    iterations_dir = rapids_path / ITERATIONS_DIR
    summary = {
        "current_iteration": get_current_iteration(rapids_path),
        "iterations": [],
    }

    if not iterations_dir.is_dir():
        return summary

    for entry in sorted(iterations_dir.iterdir()):
        if entry.is_dir() and entry.name.startswith("iter-"):
            status_file = entry / "status.json"
            if status_file.exists():
                status = read_json(status_file)
                features_dir = entry / "plan" / "features"
                if not features_dir.is_dir():
                    features_dir = entry / "features"  # legacy fallback
                feature_count = len(list(features_dir.glob("*.yaml"))) if features_dir.is_dir() else 0
                summary["iterations"].append({
                    "iteration": status.get("iteration", entry.name),
                    "status": status.get("status", "unknown"),
                    "problem_statement": status.get("problem_statement"),
                    "features": feature_count,
                    "started_at": status.get("started_at"),
                    "completed_at": status.get("completed_at"),
                })

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="RAPIDS iteration manager")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("detect", help="Check if a new iteration is needed")
    sub.add_parser("summary", help="Show all iterations summary")
    sub.add_parser("current", help="Show current iteration number")

    new_p = sub.add_parser("new", help="Create a new iteration")
    new_p.add_argument("--problem", type=str, default=None)

    args = parser.parse_args()

    from aah.core.common.config import get_active_project_aah_path
    rapids_path = get_active_project_aah_path()
    if rapids_path is None:
        print("Error: no active project", file=sys.stderr)
        sys.exit(1)

    if args.command == "detect":
        result = detect_needs_new_iteration(rapids_path)
        json.dump(result, sys.stdout, indent=2)
        print()

    elif args.command == "current":
        print(get_current_iteration(rapids_path))

    elif args.command == "summary":
        result = get_iteration_summary(rapids_path)
        json.dump(result, sys.stdout, indent=2)
        print()

    elif args.command == "new":
        result = create_new_iteration(rapids_path, args.problem)
        json.dump(result, sys.stdout, indent=2)
        print()
        print(f"Created iteration {result['iteration']}", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
