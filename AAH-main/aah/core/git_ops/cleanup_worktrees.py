#!/usr/bin/env python3
"""
Clean up orphaned git worktrees left by killed/completed subagents.

Claude Code creates worktrees at .claude/worktrees/agent-<id> when
isolation: worktree is used. They're supposed to auto-clean, but don't when:
- Agent is killed/stopped by user
- Agent made changes (by design)
- Agent hit an error

This script:
1. Lists all worktrees in a repo
2. Shows what work each contains (commits, branch)
3. Optionally removes them (with --clean flag)
4. Preserves branch refs so work isn't lost (unless --prune-branches)

Usage:
  aah run core.git_ops.cleanup_worktrees list
  aah run core.git_ops.cleanup_worktrees clean
  aah run core.git_ops.cleanup_worktrees clean --prune-branches
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def list_worktrees(repo_path: Path) -> list[dict]:
    """
    List all agent worktrees for a repo with metadata.

    Finds worktrees two ways:
    1. Via `git worktree list` — registered worktrees
    2. By scanning .claude/worktrees/ — directories that exist on disk but were
       deregistered by git (e.g. after a partial `git worktree remove` that didn't
       delete the directory). These are missed by git worktree list but still occupy
       disk and contain Claude agent-memory artifacts.
    """
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo_path, capture_output=True, text=True, check=False,
    )

    registered_paths = set()
    worktrees = []
    current = {}
    if result.returncode == 0:
        for line in result.stdout.split("\n"):
            if line.startswith("worktree "):
                if current:
                    worktrees.append(current)
                current = {"path": line.split(" ", 1)[1]}
            elif line.startswith("HEAD "):
                current["head"] = line.split(" ", 1)[1]
            elif line.startswith("branch "):
                current["branch"] = line.split(" ", 1)[1].replace("refs/heads/", "")
            elif line == "":
                if current:
                    worktrees.append(current)
                    current = {}
        if current:
            worktrees.append(current)
        registered_paths = {Path(wt["path"]) for wt in worktrees}

    # Also scan .claude/worktrees/ for directories not in git's registry.
    # These are deregistered worktrees — git removed their metadata entry but
    # left the directory behind (happens when agents are killed or when
    # `git worktree remove` is run without --force on a dirty tree).
    worktrees_dir = repo_path / ".claude" / "worktrees"
    if worktrees_dir.exists():
        for child in sorted(worktrees_dir.iterdir()):
            if child.is_dir() and child.name.startswith("agent-") and child not in registered_paths:
                worktrees.append({
                    "path": str(child),
                    "branch": "",
                    "head": "",
                    "deregistered": True,  # not in git worktree list
                })

    # Filter to agent worktrees only (skip the main worktree)
    agent_worktrees = []
    for wt in worktrees:
        path = Path(wt["path"])
        if "agent-" in path.name or "worktree-" in wt.get("branch", ""):
            # Get commit count on this branch vs main
            branch = wt.get("branch", "")
            commit_count = 0
            if branch:
                count_result = subprocess.run(
                    ["git", "rev-list", "--count", f"main..{branch}"],
                    cwd=repo_path, capture_output=True, text=True, check=False,
                )
                if count_result.returncode == 0:
                    commit_count = int(count_result.stdout.strip())

            wt["commits_ahead"] = commit_count
            wt["is_orphan"] = not Path(wt["path"]).exists() or not (Path(wt["path"]) / ".git").exists()

            # Check disk size
            if Path(wt["path"]).exists():
                size_result = subprocess.run(
                    ["du", "-sh", wt["path"]],
                    capture_output=True, text=True, check=False,
                )
                wt["disk_size"] = size_result.stdout.split("\t")[0].strip() if size_result.returncode == 0 else "?"
            else:
                wt["disk_size"] = "0"

            agent_worktrees.append(wt)

    return agent_worktrees


def clean_worktrees(
    repo_path: Path,
    prune_branches: bool = False,
    agent_id: str | None = None,
) -> list[dict]:
    """
    Remove orphaned worktrees. Returns list of cleaned entries.

    If agent_id is provided, only worktrees whose path contains
    "agent-<id>" or whose branch matches "worktree-agent-<id>" are
    removed. This is critical for the SubagentStop hook, which must
    never clean up a peer agent's still-running worktree.
    """
    import shutil
    worktrees = list_worktrees(repo_path)
    cleaned = []

    if agent_id:
        # agent_id from Claude Code is e.g. "af37a60280b585086" — the
        # worktree dir/branch uses a truncated prefix like "agent-af37a602".
        # Match both full and any 8+ char prefix.
        def _matches(wt: dict) -> bool:
            path_name = Path(wt["path"]).name  # e.g. "agent-af37a602"
            branch = wt.get("branch", "")
            for candidate in (agent_id, agent_id[:8], agent_id[:12]):
                if not candidate:
                    continue
                if candidate in path_name or candidate in branch:
                    return True
            return False
        worktrees = [wt for wt in worktrees if _matches(wt)]

    for wt in worktrees:
        wt_path = Path(wt["path"])
        branch = wt.get("branch", "")

        # Safety: never delete directories outside the repo's .claude/worktrees/
        # or git-managed worktree locations. This prevents accidental deletion
        # of workspace project directories or other user data.
        _safe_prefixes = [
            repo_path / ".claude" / "worktrees",
            repo_path / ".git" / "worktrees",
        ]
        if not any(wt_path == prefix or str(wt_path).startswith(str(prefix) + "/") for prefix in _safe_prefixes):
            print(f"cleanup_worktrees: SKIPPING {wt_path} — not inside a known worktree directory. "
                  f"Refusing to delete to prevent data loss.", file=sys.stderr)
            continue

        if wt.get("deregistered"):
            # Directory exists on disk but is not in git's worktree registry.
            # git worktree remove won't work — just delete the directory.
            shutil.rmtree(wt_path, ignore_errors=True)
        else:
            # Remove the worktree via git (preferred — updates git's metadata)
            result = subprocess.run(
                ["git", "worktree", "remove", str(wt_path), "--force"],
                cwd=repo_path, capture_output=True, text=True, check=False,
            )
            if result.returncode != 0:
                # Fallback: force delete directory and prune stale entries
                if wt_path.exists():
                    shutil.rmtree(wt_path, ignore_errors=True)
                subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=repo_path, capture_output=True, check=False,
                )

        # Optionally delete the branch
        if prune_branches and branch:
            subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=repo_path, capture_output=True, check=False,
            )
            wt["branch_pruned"] = True
        else:
            wt["branch_pruned"] = False

        wt["cleaned"] = True
        cleaned.append(wt)

    return cleaned


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean up orphaned git worktrees")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List orphaned worktrees")

    clean_p = sub.add_parser("clean", help="Remove orphaned worktrees")
    clean_p.add_argument("--prune-branches", action="store_true",
                         help="Also delete the worktree branches (work may be lost)")
    clean_p.add_argument("--agent-id", type=str, default=None,
                         help="Only clean the worktree for this agent id (scopes SubagentStop hook to one worktree)")
    clean_p.add_argument("--from-hook", action="store_true",
                         help="Read agent_id from hook JSON on stdin (for SubagentStop invocation)")

    # Optional: specify which repo (defaults to project from config)
    for s in sub.choices.values():
        s.add_argument("--repo-path", type=Path, default=None)
        s.add_argument("--all", action="store_true",
                       help="Clean both framework and project repos")

    args = parser.parse_args()

    # Determine repo paths
    repos = []
    if args.repo_path:
        repos.append(args.repo_path)
    else:
        from aah.core.common.config import resolve_project_path, resolve_framework_root
        project = resolve_project_path()
        if project is not None:
            repos.append(project)

        if args.all:
            fw = resolve_framework_root()
            if fw and fw not in repos:
                repos.append(fw)

    if not repos:
        # No project found — exit silently for hook invocations
        sys.exit(0)

    if args.command == "list":
        all_worktrees = []
        for repo in repos:
            wts = list_worktrees(repo)
            for wt in wts:
                wt["repo"] = str(repo)
            all_worktrees.extend(wts)

        json.dump(all_worktrees, sys.stdout, indent=2)
        print()

        # Human summary
        if all_worktrees:
            print(f"\n{len(all_worktrees)} orphaned worktree(s):", file=sys.stderr)
            for wt in all_worktrees:
                print(f"  {wt.get('branch', '?'):40s}  {wt.get('commits_ahead', 0):3d} commits  {wt.get('disk_size', '?'):>6s}  {wt['path']}", file=sys.stderr)
            print(f"\nRun with 'clean' to remove, or 'clean --prune-branches' to also delete branches.", file=sys.stderr)
        else:
            print("No orphaned worktrees found.", file=sys.stderr)

    elif args.command == "clean":
        # Resolve agent_id scope: explicit flag wins, else read from hook stdin.
        agent_id = args.agent_id
        if agent_id is None and args.from_hook:
            try:
                hook_input = json.load(sys.stdin)
                agent_id = hook_input.get("agent_id") or None
            except (json.JSONDecodeError, EOFError):
                agent_id = None

        # Safety: if this looks like a SubagentStop invocation but we
        # couldn't resolve an agent_id, DO NOT clean anything. Cleaning
        # all worktrees blindly was the original bug that let F017's
        # SubagentStop wipe F001's in-flight worktree.
        if args.from_hook and not agent_id:
            print("cleanup_worktrees: --from-hook invoked but no agent_id in stdin; skipping to avoid clobbering peer worktrees", file=sys.stderr)
            sys.exit(0)

        all_cleaned = []
        for repo in repos:
            cleaned = clean_worktrees(repo, args.prune_branches, agent_id=agent_id)
            for c in cleaned:
                c["repo"] = str(repo)
            all_cleaned.extend(cleaned)

        json.dump(all_cleaned, sys.stdout, indent=2)
        print()
        scope = f" (agent_id={agent_id})" if agent_id else ""
        print(f"Cleaned {len(all_cleaned)} worktree(s){scope}", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
