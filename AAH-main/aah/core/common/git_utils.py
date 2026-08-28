#!/usr/bin/env python3
"""Git operations wrapper for AAH framework."""

import argparse
import json
import subprocess
import sys
from pathlib import Path


class GitError(Exception):
    """Raised when a git operation fails."""
    pass


def run_git(args: list[str], cwd: Path | None = None, check: bool = True,
            timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run a git command and return the result."""
    cmd = ["git"] + args
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result
    except FileNotFoundError:
        raise GitError("git is not installed or not in PATH")


def init_repo(path: Path, initial_branch: str = "main") -> None:
    """Initialize a new git repository with the specified initial branch."""
    run_git(["init", "-b", initial_branch], cwd=path)


def create_branch(branch: str, cwd: Path | None = None) -> None:
    """Create a new branch (does not switch to it)."""
    run_git(["branch", branch], cwd=cwd)


def is_linked_worktree(cwd: Path | None = None) -> bool:
    """True if `cwd` is a linked worktree: its `.git` is a gitdir-pointer file,
    where the main repo's `.git` is a directory."""
    if cwd is None:
        return False
    return (cwd / ".git").is_file()


def checkout_branch(
    branch: str,
    cwd: Path | None = None,
    create: bool = False,
    commit_state: bool = True,
) -> None:
    """Switch to a branch, optionally creating it.

    With ``commit_state=True`` (default), pending ``.aah/`` bookkeeping is
    committed first.

    Skipped in linked worktrees — the blunt ``git add .aah/`` would sweep in
    other features' YAMLs and poison later merges. Pass ``commit_state=False``
    when the caller has already committed or must not create commits.
    """
    if commit_state and cwd is not None and not is_linked_worktree(cwd):
        commit_aah_state(cwd, message="chore: commit AAH state before branch switch")
    args = ["checkout"]
    if create:
        args.append("-b")
    args.append(branch)
    run_git(args, cwd=cwd)


def current_branch(cwd: Path | None = None) -> str:
    """Get the current branch name."""
    result = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    return result.stdout.strip()


def repo_common_root(cwd: Path | None = None) -> Path | None:
    """Return the main checkout root shared by all linked worktrees."""
    try:
        base = Path(cwd or Path.cwd()).resolve()
        result = run_git(["rev-parse", "--git-common-dir"], cwd=base, check=False)
    except (GitError, OSError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    try:
        return (base / Path(raw)).resolve().parent
    except OSError:
        return None


def has_commits(cwd: Path | None = None) -> bool:
    """Check if the repo has any commits."""
    result = run_git(["rev-parse", "HEAD"], cwd=cwd, check=False)
    return result.returncode == 0


def is_clean(cwd: Path | None = None) -> bool:
    """Check if the working directory is clean (no uncommitted changes)."""
    result = run_git(["status", "--porcelain"], cwd=cwd)
    return result.stdout.strip() == ""


# `.claude/` is local; only `.aah/` is eligible for automatic commits.
IDENTITY_EXCLUDE_PREFIXES = (".aah/", ".claude/")
AAH_STATE_PATHS = IDENTITY_EXCLUDE_PREFIXES
AAH_COMMIT_PATHS = (".aah/",)


def tracked_claude_dirt(cwd: Path | None = None) -> list[str]:
    """Return tracked changes under legacy ``.claude/`` paths."""
    result = run_git(
        ["status", "--porcelain", "--untracked-files=no", "--", ".claude/"],
        cwd=cwd,
        check=False,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def porcelain_dirt(
    cwd: Path | None = None,
    prefixes=IDENTITY_EXCLUDE_PREFIXES,
    *,
    tracked_only: bool = False,
) -> list[str]:
    """Porcelain lines for changes OUTSIDE `prefixes`.

    One parse for both callers: `is_clean_ignoring` wants the emptiness, and
    the regression producer wants the offending lines to report.

    `tracked_only` skips "??" entries. Callers that run a build tool want that:
    `uv run pytest` can create `uv.lock`, which is not in the scaffolded
    GITIGNORE_PYTHON, and treating that as dirt would refuse every run. A real
    source edit is a tracked modification and still shows up.
    """
    result = run_git(["status", "--porcelain"], cwd=cwd, check=False)
    dirt: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        if tracked_only and line.startswith("??"):
            continue
        entry = line[3:]
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        rel = entry.strip().strip('"').replace("\\", "/")
        if any(rel == p.rstrip("/") or rel.startswith(p) for p in prefixes):
            continue
        dirt.append(line)
    return dirt


def is_clean_ignoring(cwd: Path | None = None, prefixes=IDENTITY_EXCLUDE_PREFIXES) -> bool:
    """Clean check that ignores porcelain entries under the given prefixes."""
    return not porcelain_dirt(cwd, prefixes)


_SUBJECT_IDENTITY_CACHE: dict[tuple[str, str, tuple[str, ...]], str] = {}


def code_subject_identity(
    cwd: Path | None = None, prefixes=IDENTITY_EXCLUDE_PREFIXES, ref: str = "HEAD"
) -> str | None:
    """Merge-safe content identity of `ref`'s tree, excluding `prefixes`.

    sha256 over `git ls-tree -r <resolved-commit>` with excluded paths filtered
    out, so a commit that only touches .aah/ or .claude/ bookkeeping does not
    move it. Results are cached by repository, resolved commit, and exclusions;
    resolving `ref` on every call makes a moved ref a cache miss.
    A synthetic 64-hex token (never a real commit SHA); only ever compared,
    used as a path segment, or displayed. None if `ref` has no tree.

    Pass `ref` (e.g. "integration/wave-3") to hash a branch tip without a
    checkout — the gate side needs branch-ref semantics, not working-tree HEAD.
    """
    import hashlib

    resolved = run_git(["rev-parse", ref], cwd=cwd, check=False)
    if resolved.returncode != 0:
        return None
    commit = resolved.stdout.strip()
    normalized_prefixes = tuple(prefixes)
    repo_path = str(Path(cwd).resolve() if cwd is not None else Path.cwd().resolve())
    cache_key = (repo_path, commit, normalized_prefixes)
    cached = _SUBJECT_IDENTITY_CACHE.get(cache_key)
    if cached is not None:
        return cached

    # Hash the resolved commit, not the ref, so a concurrent ref move cannot
    # cache the new tree under the old commit's key.
    res = run_git(["ls-tree", "-r", commit, "--full-tree"], cwd=cwd, check=False)
    if res.returncode != 0:
        return None
    kept = []
    for line in res.stdout.splitlines():
        tab = line.find("\t")
        if tab == -1:
            continue
        path = line[tab + 1:].strip().strip('"').replace("\\", "/")
        if any(
            path == prefix.rstrip("/") or path.startswith(prefix)
            for prefix in normalized_prefixes
        ):
            continue
        kept.append(line)
    digest = hashlib.sha256("\n".join(kept).encode("utf-8")).hexdigest()
    _SUBJECT_IDENTITY_CACHE[cache_key] = digest
    return digest


def has_uncommitted_changes(cwd: Path | None = None) -> bool:
    """Check if there are uncommitted changes (staged or unstaged)."""
    return not is_clean(cwd)


def add_all(cwd: Path | None = None) -> None:
    """Stage all changes."""
    run_git(["add", "-A"], cwd=cwd)


def commit(message: str, cwd: Path | None = None) -> str:
    """Create a commit with the given message. Returns the commit hash."""
    run_git(["commit", "-m", message], cwd=cwd)
    result = run_git(["rev-parse", "HEAD"], cwd=cwd)
    return result.stdout.strip()


def push_to_remote(cwd: Path, remote: str = "origin", branch: str = "HEAD", tags: bool = False) -> bool:
    """
    Push the current branch (or specified ref) to remote.

    Returns True if push succeeded, False if remote is unreachable or push fails.
    Non-fatal — callers log the failure but do not abort.
    """
    args = ["push", remote, branch]
    result = run_git(args, cwd=cwd, check=False)
    if result.returncode != 0:
        print(f"Warning: git push {remote} {branch} failed: {result.stderr.strip()}", file=sys.stderr)
        return False
    if tags:
        tag_result = run_git(["push", "--tags", remote], cwd=cwd, check=False)
        if tag_result.returncode != 0:
            print(f"Warning: git push --tags {remote} failed: {tag_result.stderr.strip()}", file=sys.stderr)
            return False
    return True


def _merge_in_progress(cwd: Path | None = None) -> bool:
    """True if a merge is in progress.

    `rev-parse -q --verify` needs no path resolution, so it behaves identically
    in the main repo and in a worktree (where .git is a file, not a directory —
    see check_clean_git.py:_is_worktree).
    """
    return run_git(
        ["rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=cwd, check=False
    ).returncode == 0


def commit_aah_state(
    cwd: Path,
    message: str = "chore: commit AAH state before branch operation",
    paths=AAH_COMMIT_PATHS,
) -> bool:
    """
    Commit any pending changes under `paths` so branch switches don't fail.

    AAH state files (progress, audit logs, test results) are updated
    in-place during sessions and often left uncommitted. Any git checkout
    will fail if these files are modified. This function commits them first.

    The commit is pathspec-scoped (`git commit --only -- <paths>`), so a
    pre-existing staged application file is never swept into a framework
    bookkeeping commit.

    Returns True if a commit was made, False if there was nothing to commit,
    a merge is in progress, or the commit itself failed.
    """
    if _merge_in_progress(cwd):
        # ponytail: refuse, never complete someone else's merge. `git commit
        # --only` is a hard error mid-merge, and a plain commit would author a
        # merge commit the caller never asked for. Callers treat False as
        # non-fatal and re-check cleanliness afterwards.
        print(
            "commit_aah_state: merge in progress — deferring state commit",
            file=sys.stderr,
        )
        return False

    # One `add` per path: a pathspec matching nothing (e.g. a project with no
    # .claude/) aborts the ENTIRE add when they are passed together, silently
    # staging nothing.
    for path in paths:
        run_git(["add", "-A", "--", path], cwd=cwd, check=False)

    staged = run_git(
        ["diff", "--cached", "--name-only", "-z", "--", *paths], cwd=cwd, check=False
    )
    staged_files = [p for p in staged.stdout.split("\0") if p]
    if not staged_files:
        return False

    # Commit the ACTUAL staged paths, not the prefixes: `git commit --only` also
    # errors on a prefix git knows nothing about. Scoping to real paths keeps
    # unrelated staged application files out of this commit.
    res = run_git(
        ["commit", "--only", "-m", message, "--", *staged_files], cwd=cwd, check=False
    )
    if res.returncode != 0:
        print(
            f"commit_aah_state: commit failed: {res.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    return True


def get_log(cwd: Path | None = None, count: int = 20) -> list[dict]:
    """Get recent git log entries as structured data.

    `date` uses %aI (strict ISO 8601) so callers can pass it straight to
    datetime.fromisoformat. %ai's space separator sorts below 'T', which
    silently broke string compares against claude-progress.json timestamps.
    """
    fmt = '{"hash": "%H", "short_hash": "%h", "subject": "%s", "author": "%an", "date": "%aI"}'
    result = run_git(
        ["log", f"-{count}", f"--pretty=format:{fmt}"],
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    entries = []
    for line in result.stdout.strip().split("\n"):
        if line.strip():
            entries.append(json.loads(line))
    return entries


def get_branches(cwd: Path | None = None) -> list[str]:
    """List all local branches."""
    result = run_git(["branch", "--format=%(refname:short)"], cwd=cwd)
    return [b.strip() for b in result.stdout.strip().split("\n") if b.strip()]


def branch_exists(branch: str, cwd: Path | None = None) -> bool:
    """Check if a branch exists locally."""
    return branch in get_branches(cwd)


def merge_branch(source: str, cwd: Path | None = None, no_ff: bool = False) -> None:
    """Merge a source branch into the current branch."""
    args = ["merge"]
    if no_ff:
        args.append("--no-ff")
    args.append(source)
    run_git(args, cwd=cwd)


def fast_forward_merge(source: str, cwd: Path | None = None) -> None:
    """Fast-forward merge a source branch into the current branch."""
    run_git(["merge", "--ff-only", source], cwd=cwd)


def delete_branch(branch: str, cwd: Path | None = None, force: bool = False) -> None:
    """Delete a local branch."""
    flag = "-D" if force else "-d"
    run_git(["branch", flag, branch], cwd=cwd)


def rev_parse(ref: str, cwd: Path | None = None) -> str | None:
    """Resolve a ref to its full SHA, or None if the ref doesn't exist."""
    result = run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=cwd, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def is_ancestor(ancestor: str, descendant: str, cwd: Path | None = None) -> bool:
    """Return True if `ancestor` is an ancestor of `descendant` (i.e. reachable)."""
    result = run_git(
        ["merge-base", "--is-ancestor", ancestor, descendant],
        cwd=cwd, check=False,
    )
    return result.returncode == 0


def branch_contains_commit(
    branch: str, sha: str, cwd: Path | None = None
) -> bool:
    """Return True if ``sha`` is reachable from ``branch``'s tip."""
    if not branch_exists(branch, cwd=cwd):
        return False
    return is_ancestor(sha, branch, cwd=cwd)


# Conventional-commit subject prefixes we accept as "implements feature FNNN"
_FEATURE_COMMIT_PREFIXES = ("feat", "fix", "refactor", "perf", "test", "chore")


def find_feature_commits(feature_id: str, cwd: Path | None = None) -> list[str]:
    """
    Find every commit across all refs that implements the given feature.

    Search strategy (manifest-first to prefer authoritative source):
    1. feature-commits.json manifest (written by implement phase) - AUTHORITATIVE
    2. Conventional-commit subjects: feat(FNNN): ..., fix(FNNN): ..., etc.
    3. Feature ID mentioned anywhere in commit message (e.g. "implement F005")

    Returns a list of full SHAs (most recent first, deduped). Used by the
    merge/promote scripts to verify that a feature's work is actually
    reachable from a candidate base branch.

    The manifest is checked first because it represents the authoritative record
    of which commits belong to a feature, avoiding issues with duplicate
    implementations or commits on multiple branches.
    """
    seen: set[str] = set()
    out: list[str] = []

    def _collect(grep_pattern: str) -> None:
        result = run_git(
            [
                "log", "--all", "--no-merges",
                f"--grep={grep_pattern}", "--extended-regexp",
                "--pretty=format:%H",
            ],
            cwd=cwd, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                sha = line.strip()
                if sha and sha not in seen:
                    seen.add(sha)
                    out.append(sha)

    # Strategy 1: feature-commits.json manifest (AUTHORITATIVE - check first)
    if cwd:
        manifest_path = cwd / ".aah" / "build" / "feature-commits.json"
        if manifest_path.exists():
            try:
                import json as _json
                data = _json.loads(manifest_path.read_text(encoding='utf-8'))
                for sha in data.get(feature_id, []):
                    if sha and sha not in seen:
                        seen.add(sha)
                        out.append(sha)
            except Exception:
                pass

    # Strategy 2: conventional-commit prefix  e.g. feat(F005):
    # Only search git log if manifest didn't have this feature
    if not out:
        alt = "|".join(_FEATURE_COMMIT_PREFIXES)
        _collect(rf"^({alt})\({feature_id}\):")

    # Strategy 3: feature ID anywhere in commit message (case-insensitive
    # word boundary match to avoid false positives like F00 matching F005)
    if not out:
        _collect(rf"\b{feature_id}\b")

    return out


def branch_contains_all_commits(branch: str, shas: list[str], cwd: Path | None = None) -> bool:
    """True iff every SHA in `shas` is reachable from `branch`'s tip."""
    if not shas:
        # Vacuously true — but callers usually want to treat empty-shas
        # as "no evidence the feature exists" and handle that separately.
        return True
    if not branch_exists(branch, cwd=cwd):
        return False
    for sha in shas:
        if not is_ancestor(sha, branch, cwd=cwd):
            return False
    return True


def list_worktrees(cwd: Path | None = None) -> list[dict]:
    """List all worktrees."""
    result = run_git(["worktree", "list", "--porcelain"], cwd=cwd, check=False)
    if result.returncode != 0:
        return []
    worktrees = []
    current: dict = {}
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
    return worktrees


def main() -> None:
    parser = argparse.ArgumentParser(description="AAH git utilities")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Check if working directory is clean")
    sub.add_parser("branch", help="Show current branch")
    sub.add_parser("branches", help="List all branches")

    log_p = sub.add_parser("log", help="Show recent commits")
    log_p.add_argument("--count", type=int, default=20)

    sub.add_parser("worktrees", help="List worktrees")

    checkout_p = sub.add_parser("checkout", help="Switch branch, committing pending .aah/ state first")
    checkout_p.add_argument("branch", help="Branch to switch to")
    checkout_p.add_argument("--project-path", type=Path, default=None)
    checkout_p.add_argument("--create", action="store_true", help="Create the branch (git checkout -b)")

    args = parser.parse_args()

    if args.command == "status":
        clean = is_clean()
        status = "clean" if clean else "dirty"
        json.dump({"clean": clean, "status": status}, sys.stdout, indent=2)
        print()
        sys.exit(0 if clean else 2)

    elif args.command == "branch":
        print(current_branch())

    elif args.command == "branches":
        json.dump(get_branches(), sys.stdout, indent=2)
        print()

    elif args.command == "log":
        entries = get_log(count=args.count)
        json.dump(entries, sys.stdout, indent=2)
        print()

    elif args.command == "worktrees":
        wts = list_worktrees()
        json.dump(wts, sys.stdout, indent=2)
        print()

    elif args.command == "checkout":
        from aah.core.common.config import require_project_path # local: keep module stdlib-only
        project_path = require_project_path(args.project_path)
        try:
            checkout_branch(args.branch, cwd=project_path, create=args.create)
        except GitError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        print(current_branch(cwd=project_path))

    sys.exit(0)


if __name__ == "__main__":
    main()
