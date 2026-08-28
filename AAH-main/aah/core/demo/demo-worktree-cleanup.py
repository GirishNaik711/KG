#!/usr/bin/env python3
"""
RAPIDS Demo Worktree Cleanup.

Removes demo worktrees and their associated branches created by
demo-worktree-setup.py.

Usage:
    python3 demo-worktree-cleanup.py <project-path> [<project-name>]

    <project-path>  Path to the project directory (used to find the correct
                     git repo). Can be a harness projects/ path or a workspace path.
    <project-name>  If provided, only removes worktrees for that project.
                    Otherwise, removes all demo worktrees (branches matching demo/*).

Examples:
    python3 demo-worktree-cleanup.py projects/my-project my-project
    python3 demo-worktree-cleanup.py /path/to/workspace/my-project my-project
"""

import os
import shutil
import subprocess
import sys

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def find_git_root(cwd=None):
    """Find the git repository root directory.

    Args:
        cwd: Directory to run the git command from. If provided, finds
             the git root for the repo containing that directory.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
            cwd=cwd
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        print(f"Error: Not inside a git repository (cwd={cwd}).")
        sys.exit(1)


def list_demo_worktrees(git_root, project_filter=None):
    """List all demo worktrees, optionally filtered by project name.

    Demo worktrees are identified by path — they live under
    .claude/worktrees/ in the project directory. They use detached HEAD
    (no named branch), so detection is path-based.

    Also detects legacy demo worktrees that used named demo/* branches.

    Returns list of (worktree_path, branch_name_or_none) tuples.
    """
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True, text=True, cwd=git_root
    )

    worktrees = []
    current_path = None
    current_branch = None
    is_detached = False

    for line in result.stdout.split("\n"):
        if line.startswith("worktree "):
            current_path = line[len("worktree "):]
            current_branch = None
            is_detached = False
        elif line.startswith("branch refs/heads/"):
            current_branch = line[len("branch refs/heads/"):]
        elif line == "detached":
            is_detached = True
        elif line == "":
            if current_path:
                is_demo = False
                # Detect by path: .claude/worktrees/<phase>
                if ".claude/worktrees/" in current_path or ".claude\\worktrees\\" in current_path:
                    is_demo = True
                # Detect legacy named demo branches
                elif current_branch and current_branch.startswith("demo/"):
                    is_demo = True

                if is_demo:
                    if project_filter:
                        # Filter by project name in the path
                        if project_filter in current_path:
                            worktrees.append((current_path, current_branch))
                        elif current_branch and f"/{project_filter}/" in current_branch:
                            worktrees.append((current_path, current_branch))
                    else:
                        worktrees.append((current_path, current_branch))
            current_path = None
            current_branch = None
            is_detached = False

    return worktrees


def remove_worktree(git_root, worktree_path, branch_name=None):
    """Remove a worktree and optionally its branch.

    For detached HEAD worktrees (branch_name is None), only the worktree
    is removed. For legacy named-branch worktrees, the branch is also deleted.
    """
    # Remove worktree
    result = subprocess.run(
        ["git", "worktree", "remove", worktree_path, "--force"],
        capture_output=True, text=True, cwd=git_root
    )
    if result.returncode != 0:
        # Worktree might already be gone — try cleaning up the directory
        if os.path.exists(worktree_path):
            import shutil
            shutil.rmtree(worktree_path, ignore_errors=True)
        # Prune stale worktree entries
        subprocess.run(
            ["git", "worktree", "prune"],
            capture_output=True, text=True, cwd=git_root
        )

    # Delete branch only if one exists (legacy named-branch worktrees)
    if branch_name:
        result = subprocess.run(
            ["git", "branch", "-D", branch_name],
            capture_output=True, text=True, cwd=git_root
        )
        if result.returncode != 0:
            print(f"  Warning: Could not delete branch {branch_name}: {result.stderr.strip()}")


def main():
    if len(sys.argv) < 2:
        print("Usage: demo-worktree-cleanup.py <project-path> [<project-name>]")
        sys.exit(1)

    project_path = os.path.abspath(sys.argv[1])
    project_filter = sys.argv[2] if len(sys.argv) >= 3 else None

    # Use the project path to find the correct git root
    git_root = find_git_root(cwd=project_path)
    worktrees = list_demo_worktrees(git_root, project_filter)

    # Also find plain directories (e.g. onboarding) under .claude/worktrees/
    # that are not git worktrees — these are created by setup_onboarding_directory.
    plain_dirs = []
    worktrees_dir = os.path.join(project_path, ".claude", "worktrees")
    if os.path.isdir(worktrees_dir):
        git_wt_paths = {wt_path.replace("\\", "/") for wt_path, _ in worktrees}
        for entry in os.listdir(worktrees_dir):
            entry_path = os.path.join(worktrees_dir, entry)
            if os.path.isdir(entry_path):
                norm = entry_path.replace("\\", "/")
                if norm not in git_wt_paths:
                    if not project_filter or project_filter in entry_path:
                        plain_dirs.append(entry_path)

    total = len(worktrees) + len(plain_dirs)
    if total == 0:
        scope = f"project '{project_filter}'" if project_filter else "any project"
        print(f"No demo worktrees found for {scope}.")
        return

    print(f"Found {total} demo worktree(s):\n")

    for wt_path, branch in worktrees:
        label = branch if branch else os.path.basename(wt_path)
        print(f"  Removing: {label}")
        print(f"    Path: {wt_path}")
        remove_worktree(git_root, wt_path, branch)
        print(f"    Done.")

    for plain_path in plain_dirs:
        label = os.path.basename(plain_path)
        print(f"  Removing: {label} (plain directory)")
        print(f"    Path: {plain_path}")
        shutil.rmtree(plain_path, ignore_errors=True)
        print(f"    Done.")

    print(f"\nCleanup complete. Removed {total} worktree(s).")


if __name__ == "__main__":
    main()
