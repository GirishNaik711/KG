#!/usr/bin/env python3
"""
Optional tmux convenience layer for RAPIDS demos.

Creates a tmux session with one window per phase, each cd'd into
the corresponding phase worktree. Works on top of phase_checkout.py.

Usage:
  aah run core.demo.tmux_launcher launch --project-path PATH
  aah run core.demo.tmux_launcher teardown [--session-name NAME]
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


DEFAULT_SESSION = "rapids-demo"


def _tmux(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux"] + args, capture_output=True, text=True, check=False,
    )


def launch(project_path: Path, session_name: str = DEFAULT_SESSION) -> bool:
    """
    Create a tmux session with phase worktrees.

    Returns True on success, False if tmux not available or setup failed.
    """
    # Check tmux is installed
    if shutil.which("tmux") is None:
        print(
            "tmux not found — use `aah run core.demo.phase_checkout setup` "
            "and open terminals manually.",
            file=sys.stderr,
        )
        return False

    # Ensure worktrees exist
    from aah.core.demo.phase_checkout import setup, list_phases
    worktrees = setup(project_path)
    if not worktrees:
        print("No worktrees created — no phase tags found.", file=sys.stderr)
        return False

    active_worktrees = [wt for wt in worktrees if wt["status"] in ("created", "exists")]
    if not active_worktrees:
        print("No active worktrees available.", file=sys.stderr)
        return False

    # Kill existing session if present
    _tmux(["kill-session", "-t", session_name])

    # Create new session with first worktree
    first = active_worktrees[0]
    result = _tmux([
        "new-session", "-d", "-s", session_name,
        "-c", first["path"],
        "-n", first["phase"],
    ])
    if result.returncode != 0:
        print(f"Failed to create tmux session: {result.stderr}", file=sys.stderr)
        return False

    # Send intro command to first window
    _tmux(["send-keys", "-t", f"{session_name}:{first['phase']}",
           f"echo '=== RAPIDS Demo: {first['phase']} phase (tag: {first['tag']}) ==='", "Enter"])

    # Create additional windows
    for wt in active_worktrees[1:]:
        _tmux([
            "new-window", "-t", session_name,
            "-c", wt["path"],
            "-n", wt["phase"],
        ])
        _tmux(["send-keys", "-t", f"{session_name}:{wt['phase']}",
               f"echo '=== RAPIDS Demo: {wt['phase']} phase (tag: {wt['tag']}) ==='", "Enter"])

    # Select first window
    _tmux(["select-window", "-t", f"{session_name}:{active_worktrees[0]['phase']}"])

    print(f"tmux session '{session_name}' created with {len(active_worktrees)} window(s).", file=sys.stderr)
    print(f"Attach with: tmux attach -t {session_name}", file=sys.stderr)
    return True


def teardown(session_name: str = DEFAULT_SESSION, project_path: Path | None = None) -> None:
    """Kill tmux session and optionally clean up worktrees."""
    _tmux(["kill-session", "-t", session_name])
    print(f"Killed tmux session '{session_name}'", file=sys.stderr)

    if project_path:
        from aah.core.demo.phase_checkout import cleanup
        count = cleanup(project_path)
        print(f"Cleaned up {count} worktree(s)", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="RAPIDS tmux demo launcher")
    sub = parser.add_subparsers(dest="command", required=True)

    launch_p = sub.add_parser("launch", help="Launch tmux demo session")
    launch_p.add_argument("--project-path", type=Path, default=None)
    launch_p.add_argument("--session-name", type=str, default=DEFAULT_SESSION)

    td_p = sub.add_parser("teardown", help="Kill tmux session and clean up")
    td_p.add_argument("--session-name", type=str, default=DEFAULT_SESSION)
    td_p.add_argument("--project-path", type=Path, default=None)

    args = parser.parse_args()

    if args.command == "launch":
        from aah.core.common.config import require_project_path
        project_path = require_project_path(args.project_path)
        success = launch(project_path, args.session_name)
        sys.exit(0 if success else 1)

    elif args.command == "teardown":
        pp = None
        if args.project_path:
            pp = args.project_path
        teardown(args.session_name, pp)
        sys.exit(0)


if __name__ == "__main__":
    main()
