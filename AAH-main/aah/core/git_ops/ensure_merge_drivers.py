#!/usr/bin/env python3
"""Make `merge=ours` on the AAH state files actually work.

Parallel feature worktrees each rewrite `claude-progress.json` and
`feature-list.json`, so the wave merge sees the same lines changed two ways and
stops on a conflict in files no human reads — pulling in aah-merge-resolver to
hand-edit the record of what is done. Keeping the integration branch's copy is
correct because that copy is authoritative: `_ensure_feature_marked_passing`
writes the `passes` flag on integration after the merge, and
`sync_features_from_yaml` re-derives feature-list.json from the YAMLs once the
wave lands.

Two things must both be true for git to resolve it that way, and neither is
versioned — so both are asserted at runtime rather than written once at
scaffold time:

1. A driver named `ours` registered in the repo config. `merge.ours.driver` is
   not something git ships; unregistered, git silently falls back to a normal
   three-way merge and the attribute reads as if it works while doing nothing.
   `true` is git's documented no-op driver: it exits 0 without touching the
   merged result, leaving %A — our side, the branch being merged into — in
   place.
2. The `merge=ours` attributes themselves. A scaffolded project commits them in
   `.gitattributes` (see ``GITATTRIBUTES_BASE`` in
   ``aah.core.scaffold.project``), but projects scaffolded before those lines
   existed have a `.gitattributes` without them, and scaffold never backfills an
   existing file. So the same lines are also written to
   `$GIT_DIR/info/attributes`, which git consults at the highest precedence.
   That file is untracked, shared across linked worktrees, and identical on
   every branch — so asserting it needs no commit, cannot dirty the working
   tree, and cannot change the code subject that regression evidence binds to.
   Committing `.gitattributes` mid-build would fail all three.

Git reads both at merge time, so asserting them also fixes waves already in
flight.

Usage:
  aah run core.git_ops.ensure_merge_drivers

Exit codes:
  0  always — this hook never blocks SessionStart.
"""

from __future__ import annotations

import sys
from pathlib import Path

from aah.core.common.git_utils import GitError, run_git


# Driver name -> command git runs to resolve the merge. `true` is a no-op that
# leaves our side in place; the name must match the `merge=<name>` attribute.
MERGE_DRIVERS = {"ours": "true"}

# The attribute lines, kept in sync with GITATTRIBUTES_BASE in
# aah.core.scaffold.project — that string is the committed, human-facing copy
# for new projects; this one is the runtime backfill for every project.
MERGE_ATTRIBUTES = (
    ".aah/codebase-intel/codemap.db merge=ours binary",
    ".aah/feature-list.json merge=ours",
    ".aah/claude-progress.json merge=ours",
    ".aah/audit/token-usage.json merge=ours",
)

_BLOCK_BEGIN = "# BEGIN AAH merge attributes (managed by aah — edits are overwritten)"
_BLOCK_END = "# END AAH merge attributes"


def _managed_block() -> str:
    return "\n".join((_BLOCK_BEGIN, *MERGE_ATTRIBUTES, _BLOCK_END)) + "\n"


def git_common_dir(project_path: Path) -> Path:
    """Resolve `$GIT_DIR` shared by the main checkout and all linked worktrees."""
    result = run_git(["rev-parse", "--git-common-dir"], cwd=project_path, check=False)
    raw = result.stdout.strip()
    if result.returncode != 0 or not raw:
        raise GitError(f"{project_path} is not a git repository")
    return (project_path / raw).resolve()


def ensure_merge_drivers(project_path: Path) -> list[str]:
    """Register the merge drivers the attributes refer to. Return those written.

    Idempotent: each value is read back first, so a call against an
    already-configured project does not touch `.git/config` at all. Returns
    the driver names that were actually written (empty when all were current).
    """
    written = []
    for name, command in MERGE_DRIVERS.items():
        key = f"merge.{name}.driver"
        current = run_git(
            ["config", "--local", "--get", key], cwd=project_path, check=False
        )
        if current.returncode == 0 and current.stdout.strip() == command:
            continue
        # --local, not --worktree: the driver must apply to every linked
        # worktree, and linked worktrees share the repository config.
        run_git(["config", "--local", key, command], cwd=project_path)
        written.append(name)
    return written


def ensure_merge_attributes(project_path: Path) -> bool:
    """Write the managed attribute block into `$GIT_DIR/info/attributes`.

    Returns True when the file was written. Idempotent, and never clobbers
    unrelated entries: an existing managed block is replaced in place, anything
    else in the file is preserved.
    """
    attributes_path = git_common_dir(project_path) / "info" / "attributes"
    block = _managed_block()

    existing = ""
    if attributes_path.exists():
        existing = attributes_path.read_text(encoding="utf-8")

    begin = existing.find(_BLOCK_BEGIN)
    end = existing.find(_BLOCK_END)
    if begin != -1 and end != -1 and end > begin:
        merged = existing[:begin] + block + existing[end + len(_BLOCK_END) :].lstrip("\n")
    elif existing.strip():
        merged = existing.rstrip("\n") + "\n\n" + block
    else:
        merged = block

    if merged == existing:
        return False

    attributes_path.parent.mkdir(parents=True, exist_ok=True)
    attributes_path.write_text(merged, encoding="utf-8")
    return True


def ensure_merge_config(project_path: Path) -> None:
    """Assert both halves: the attributes and the driver that honours them.

    Raises ``GitError`` if the path is not a git repository or git rejects a
    write. Callers decide whether that is fatal; for build machinery it is not
    — the cost is avoidable conflicts, not a broken wave.
    """
    ensure_merge_attributes(project_path)
    ensure_merge_drivers(project_path)


def main() -> None:
    from aah.core.common.config import resolve_project_path

    project_path = resolve_project_path()
    if project_path is None:
        # Pre-scaffold — no project whose merges we could be protecting.
        sys.exit(0)

    try:
        ensure_merge_config(project_path)
    except (GitError, OSError) as exc:
        # Non-blocking. The cost of failure is conflicts in .aah state files
        # during the next wave merge, not a broken session. A repo that does
        # not exist yet (`git init` happens during scaffold) lands here too and
        # is corrected by the next session start.
        print(
            f"⚠ aah: could not register git merge config: {exc}\n"
            f"  Wave merges may raise conflicts in .aah/ state files. "
            f"Fix with: git config merge.ours.driver true",
            file=sys.stderr,
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
