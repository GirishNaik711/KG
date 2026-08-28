#!/usr/bin/env python3
"""
Phase worktree checkout for RAPIDS demos.

Terminal-agnostic core — creates git worktrees at phase tags so each phase
can be explored in any terminal (iTerm2, VS Code, Warp, Alacritty, etc.).

Usage:
  aah run core.demo.phase_checkout list-phases --project-path PATH
  aah run core.demo.phase_checkout setup --project-path PATH [--phases research,analysis,plan,implement]
  aah run core.demo.phase_checkout cleanup --project-path PATH
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


WORKTREE_PREFIX = "demo-"
WORKTREE_DIR = ".claude/worktrees"

# Phases in display order
PHASE_ORDER = ["research", "analysis", "plan", "implement", "deploy", "sustain"]


def _git(args: list, cwd: Path) -> str:
    result = subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


def _get_project_name(project_path: Path) -> str:
    """Get project name from manifest."""
    manifest_path = project_path / ".rapids" / "manifest.yaml"
    if manifest_path.exists():
        try:
            import yaml
            data = yaml.safe_load(manifest_path.read_text(encoding='utf-8'))
            return data.get("project_name", "")
        except Exception:
            pass
    return ""


def list_phases(project_path: Path) -> list[dict]:
    """List all phase tags with metadata."""
    project_name = _get_project_name(project_path)
    if not project_name:
        return []

    # Get all tags matching the RAPIDS pattern
    tag_output = _git(["tag", "-l", f"{project_name}/rapids-*"], cwd=project_path)
    if not tag_output:
        return []

    phases = []
    for tag in tag_output.splitlines():
        tag = tag.strip()
        if not tag:
            continue

        # Extract phase name from tag
        phase_suffix = tag.replace(f"{project_name}/rapids-", "")

        # Get tag date and message
        date = _git(["log", "-1", "--format=%aI", tag], cwd=project_path)
        message = _git(["tag", "-l", "-n1", tag], cwd=project_path)
        commit = _git(["rev-parse", "--short", tag], cwd=project_path)

        phases.append({
            "tag": tag,
            "phase": phase_suffix,
            "date": date[:10] if date else "unknown",
            "commit": commit,
            "message": message,
        })

    # Sort by phase order
    def sort_key(p):
        name = p["phase"].split("-")[0]  # Handle implement-wave-1
        if name in PHASE_ORDER:
            return PHASE_ORDER.index(name)
        return len(PHASE_ORDER)

    phases.sort(key=sort_key)
    return phases


def setup(project_path: Path, phases: list[str] | None = None) -> list[dict]:
    """
    Create worktrees at phase tags.

    Returns list of created worktree info dicts.
    """
    available = list_phases(project_path)
    if not available:
        print("No phase tags found. Complete RAPIDS phases to create tags.", file=sys.stderr)
        return []

    # Filter to requested phases if specified
    if phases:
        phase_set = set(phases)
        available = [p for p in available if any(
            p["phase"].startswith(ph) for ph in phase_set
        )]

    worktree_base = project_path / WORKTREE_DIR
    worktree_base.mkdir(parents=True, exist_ok=True)

    created = []
    for phase_info in available:
        wt_name = f"{WORKTREE_PREFIX}{phase_info['phase']}"
        wt_path = worktree_base / wt_name

        if wt_path.exists():
            # Already exists — skip
            created.append({
                "phase": phase_info["phase"],
                "path": str(wt_path),
                "tag": phase_info["tag"],
                "status": "exists",
            })
            continue

        # Create worktree at the tag
        result = subprocess.run(
            ["git", "worktree", "add", str(wt_path), phase_info["tag"]],
            cwd=project_path, capture_output=True, text=True, check=False,
        )
        if result.returncode == 0:
            created.append({
                "phase": phase_info["phase"],
                "path": str(wt_path),
                "tag": phase_info["tag"],
                "status": "created",
            })
        else:
            created.append({
                "phase": phase_info["phase"],
                "path": str(wt_path),
                "tag": phase_info["tag"],
                "status": f"error: {result.stderr.strip()[:100]}",
            })

    return created


def cleanup(project_path: Path) -> int:
    """Remove all demo worktrees. Returns count removed."""
    worktree_base = project_path / WORKTREE_DIR
    if not worktree_base.exists():
        return 0

    count = 0
    for wt in worktree_base.iterdir():
        if wt.is_dir() and wt.name.startswith(WORKTREE_PREFIX):
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt)],
                cwd=project_path, capture_output=True, check=False,
            )
            count += 1

    # Prune stale worktree entries
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=project_path, capture_output=True, check=False,
    )
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="RAPIDS demo phase checkout")
    sub = parser.add_subparsers(dest="command", required=True)

    list_p = sub.add_parser("list-phases", help="List available phase tags")
    list_p.add_argument("--project-path", type=Path, default=None)

    setup_p = sub.add_parser("setup", help="Create worktrees at phase tags")
    setup_p.add_argument("--project-path", type=Path, default=None)
    setup_p.add_argument("--phases", type=str, default=None,
                         help="Comma-separated phases to check out")

    cleanup_p = sub.add_parser("cleanup", help="Remove demo worktrees")
    cleanup_p.add_argument("--project-path", type=Path, default=None)

    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(args.project_path)

    if args.command == "list-phases":
        phases = list_phases(project_path)
        if not phases:
            print("No phase tags found.", file=sys.stderr)
            sys.exit(1)
        json.dump(phases, sys.stdout, indent=2)
        print()

    elif args.command == "setup":
        phase_list = args.phases.split(",") if args.phases else None
        worktrees = setup(project_path, phase_list)

        json.dump(worktrees, sys.stdout, indent=2)
        print()

        # Print human-friendly guide
        print("\nDemo worktrees ready! Open separate terminal windows and cd into:", file=sys.stderr)
        for wt in worktrees:
            if wt["status"] in ("created", "exists"):
                print(f"  {wt['phase']:15s} {wt['path']}", file=sys.stderr)
        print("\nTo continue RAPIDS from any phase, run `claude` in that directory.", file=sys.stderr)
        print("To launch all in tmux: aah run core.demo.tmux_launcher launch", file=sys.stderr)

    elif args.command == "cleanup":
        count = cleanup(project_path)
        print(f"Removed {count} demo worktree(s)", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
