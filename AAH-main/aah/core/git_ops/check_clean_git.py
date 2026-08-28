#!/usr/bin/env python3
"""
SubagentStop hook: verify no uncommitted changes.

Reads hook input from stdin. If the working directory has uncommitted
changes, auto-commits AAH framework files (.aah/) and blocks
only if non-framework files remain uncommitted (exit 2).
"""

import json
import re
import sys
from pathlib import Path

from aah.core.common.git_utils import commit_aah_state, run_git


# Files/dirs that are expected to be untracked or modified at Stop time and
# should NOT block the agent from stopping. These are temp artifacts or
# framework-written files updated by other Stop hooks (activity_logger,
# generate_session_summary, progress update) during the same Stop sequence
# that runs check_clean_git — blocking on them causes an infinite Stop loop.
IGNORED_PATTERNS = [
    ".claude/",
    ".aah/agent-memory/",
    ".aah/audit/",                             # written by activity_logger Stop hook
    ".aah/claude-progress.json",               # written by progress update + generate_session_summary
    ".aah/build/subagent-learnings.json",  # written by load_impl_context at session end
]

def _is_worktree(project_path: Path) -> bool:
    """Return True if project_path is a git worktree (not the main repo)."""
    git_path = project_path / ".git"
    return git_path.is_file()


def _get_feature_id_from_branch(project_path: Path) -> str | None:
    """Extract feature ID from current branch name (e.g. 'feature/F007' -> 'F007')."""
    result = run_git(["branch", "--show-current"], cwd=project_path, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    branch = result.stdout.strip()
    match = re.match(r"^feature/(F\d+)", branch, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def _commit_aah_state_scoped(project_path: Path, feature_id: str, message: str) -> bool:
    """
    Worktree-safe variant of commit_aah_state.

    Stages all .aah/ files EXCEPT other features' contracts, then re-adds only
    this feature's contract. This prevents stale copies of other features'
    contracts from being committed in the worktree and poisoning later merges.

    Feature contracts are `.md` files (see feature_utils / build_feature_list);
    globbing `*.yaml` here matched nothing, so the `git reset` below unstaged
    every contract and none was re-added.
    """
    result = run_git(["status", "--porcelain"], cwd=project_path, check=False)
    if not result.stdout.strip():
        return False

    run_git(["add", ".aah/"], cwd=project_path, check=False)

    features_dir = project_path / ".aah" / "plan" / "features"
    if features_dir.is_dir():
        run_git(
            ["reset", "HEAD", "--", ".aah/plan/features/"],
            cwd=project_path, check=False,
        )
        for contract in features_dir.glob("*.md"):
            if contract.stem.upper().startswith(feature_id.upper()):
                rel = contract.relative_to(project_path)
                run_git(["add", str(rel)], cwd=project_path, check=False)

    staged = run_git(["diff", "--cached", "--name-only"], cwd=project_path, check=False)
    if not staged.stdout.strip():
        return False

    run_git(["commit", "-m", message], cwd=project_path, check=False)
    return True


def check_clean_git(project_path: Path) -> tuple[bool, str]:
    """
    Check if git working directory is clean.

    1. Unconditionally auto-commits any .aah/ framework files first.
       These are written by the framework at session end and must not block.
    2. Ignores expected worktree artifacts (.claude/, agent-memory).
    3. Blocks (returns False) only if non-framework files remain uncommitted.

    Returns (is_clean, message).
    """
    try:
        # Always attempt to auto-commit .aah/ files first, unconditionally.
        # This handles the case where framework hooks (generate_session_summary,
        # activity_logger) write files AFTER check_clean_git runs, causing the
        # feedback loop. By committing at the start of every invocation we
        # ensure any pending framework writes from prior hook steps are captured.
        if _is_worktree(project_path):
            feature_id = _get_feature_id_from_branch(project_path)
            if feature_id:
                _commit_aah_state_scoped(
                    project_path,
                    feature_id,
                    message="chore: auto-commit AAH framework state files (session end)",
                )
            else:
                commit_aah_state(
                    project_path,
                    message="chore: auto-commit AAH framework state files (session end)",
                )
        else:
            commit_aah_state(
                project_path,
                message="chore: auto-commit AAH framework state files (session end)",
            )

        result = run_git(["status", "--short"], cwd=project_path)
        # NOTE: rstrip only — do NOT strip leading whitespace, since
        # `git status --short` uses two columns (XY) where unstaged
        # modifications produce a leading space (" M filename"). A blanket
        # .strip() would eat that leading space and mis-align line[3:],
        # causing IGNORED_PATTERNS matching to silently fail and producing
        # an infinite Stop-hook loop.
        changed_files = result.stdout.rstrip("\n")
        if not changed_files:
            return True, "Working directory is clean"

        # Filter out expected worktree artifacts
        significant_changes = []
        for line in changed_files.split("\n"):
            if not line:
                continue
            # git status --short format: "XY path" where XY are 2 status
            # chars (X=staged, Y=unstaged) and a single space separator,
            # so the path always starts at index 3.
            file_path = line[3:]
            if not any(file_path.startswith(p) for p in IGNORED_PATTERNS):
                significant_changes.append(line)

        if not significant_changes:
            return True, "Working directory clean (only worktree artifacts)"

        return False, "Uncommitted changes detected:\n" + "\n".join(significant_changes)
    except Exception as e:
        return True, f"Git check skipped: {e}"


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    from aah.core.common.config import resolve_project_path
    cwd = hook_input.get("cwd")
    project_path = resolve_project_path(Path(cwd) if cwd else None)
    if project_path is None:
        # Not a AAH project, fall back to cwd
        project_path = Path(cwd or ".").resolve()

    is_clean, message = check_clean_git(project_path)

    if not is_clean:
        print(message, file=sys.stderr)
        print(
            "\nAAH policy: never leave uncommitted work at the end of a feature or session. "
            "Commit your changes before stopping.",
            file=sys.stderr,
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
