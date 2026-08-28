"""Tests for aah.core.common.git_utils."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from aah.core.common.git_utils import (
    AAH_STATE_PATHS,
    GitError,
    add_all,
    branch_exists,
    checkout_branch,
    commit,
    commit_aah_state,
    create_branch,
    current_branch,
    delete_branch,
    get_branches,
    get_log,
    has_commits,
    has_uncommitted_changes,
    init_repo,
    is_clean,
    is_linked_worktree,
    is_clean_ignoring,
    run_git,
)


GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@test.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@test.com",
}


class TestRunGit:
    def test_successful_command(self, git_repo):
        result = run_git(["status"], cwd=git_repo)
        assert result.returncode == 0

    def test_failed_command_raises(self, git_repo):
        with pytest.raises(GitError):
            run_git(["checkout", "nonexistent-branch"], cwd=git_repo)

    def test_failed_command_no_check(self, git_repo):
        result = run_git(["checkout", "nonexistent-branch"], cwd=git_repo, check=False)
        assert result.returncode != 0


class TestInitRepo:
    def test_init_creates_git_dir(self, tmp_path):
        repo = tmp_path / "new-repo"
        repo.mkdir()
        init_repo(repo)
        assert (repo / ".git").is_dir()


class TestBranching:
    def test_current_branch(self, git_repo):
        assert current_branch(git_repo) == "main"

    def test_create_and_checkout_branch(self, git_repo):
        create_branch("feature-1", cwd=git_repo)
        checkout_branch("feature-1", cwd=git_repo)
        assert current_branch(git_repo) == "feature-1"

    def test_checkout_create(self, git_repo):
        checkout_branch("feature-2", cwd=git_repo, create=True)
        assert current_branch(git_repo) == "feature-2"

    def test_get_branches(self, git_repo):
        create_branch("develop", cwd=git_repo)
        branches = get_branches(git_repo)
        assert "main" in branches
        assert "develop" in branches

    def test_branch_exists(self, git_repo):
        assert branch_exists("main", git_repo)
        assert not branch_exists("nonexistent", git_repo)

    def test_delete_branch(self, git_repo):
        create_branch("to-delete", cwd=git_repo)
        assert branch_exists("to-delete", git_repo)
        delete_branch("to-delete", cwd=git_repo)
        assert not branch_exists("to-delete", git_repo)


class TestCommitting:
    def test_has_commits(self, git_repo):
        assert has_commits(git_repo)

    def test_no_commits(self, tmp_path):
        repo = tmp_path / "empty-repo"
        repo.mkdir()
        init_repo(repo)
        assert not has_commits(repo)

    def test_is_clean(self, git_repo):
        assert is_clean(git_repo)

    def test_has_uncommitted_changes(self, git_repo):
        (git_repo / "new_file.txt").write_text("hello")
        assert has_uncommitted_changes(git_repo)

    def test_add_and_commit(self, git_repo):
        (git_repo / "new_file.txt").write_text("hello")
        add_all(cwd=git_repo)
        # Need to set git config for commit
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=git_repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=git_repo, capture_output=True)
        hash = commit("Add new file", cwd=git_repo)
        assert len(hash) == 40
        assert is_clean(git_repo)


def _configure_git(repo: Path) -> None:
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)


def _seed_tracked_aah_state(repo: Path) -> Path:
    """Commit a tracked .aah/ state file, mirroring a real AAH project."""
    _configure_git(repo)
    aah = repo / ".aah"
    (aah / "audit").mkdir(parents=True, exist_ok=True)
    (aah / "claude-progress.json").write_text('{"current_wave": 0}\n')
    (aah / "audit" / "token-usage.json").write_text('{"session_count": 1}\n')
    add_all(cwd=repo)
    commit("Add AAH state", cwd=repo)
    return aah


def _seed_diverged_branch(repo: Path, branch: str = "develop") -> None:
    """Create `branch` whose .aah/ state differs from the current branch.

    Divergence is essential: git only refuses a checkout when the dirty file's
    content actually differs between the two branches. Without it these tests
    would pass even with the guard removed.
    """
    start = current_branch(repo)
    aah = repo / ".aah"
    checkout_branch(branch, cwd=repo, create=True, commit_state=False)
    (aah / "claude-progress.json").write_text('{"current_wave": 99}\n')
    (aah / "audit" / "token-usage.json").write_text('{"session_count": 999}\n')
    (aah / "audit" / "activity-log.jsonl").write_text('{"event": "other-branch"}\n')
    add_all(cwd=repo)
    commit(f"Diverge {branch} AAH state", cwd=repo)
    checkout_branch(start, cwd=repo, commit_state=False)


class TestCheckoutStateGuard:
    """checkout_branch must not fail when Stop-hook state files are dirty.

    Regression guard for: "error: Your local changes to the following files
    would be overwritten by checkout: .aah/claude-progress.json".
    """

    def test_bare_git_checkout_would_fail(self, git_repo):
        """Baseline: proves the scenario really is the reported failure mode."""
        aah = _seed_tracked_aah_state(git_repo)
        _seed_diverged_branch(git_repo)
        (aah / "claude-progress.json").write_text('{"current_wave": 3}\n')

        result = run_git(["checkout", "develop"], cwd=git_repo, check=False)

        assert result.returncode != 0
        assert "would be overwritten by checkout" in result.stderr
        assert current_branch(git_repo) == "main"

    def test_checkout_succeeds_with_dirty_progress_file(self, git_repo):
        aah = _seed_tracked_aah_state(git_repo)
        _seed_diverged_branch(git_repo)

        # Simulate the Stop hook rewriting progress after the branch diverged
        (aah / "claude-progress.json").write_text('{"current_wave": 3}\n')
        assert has_uncommitted_changes(git_repo)

        checkout_branch("develop", cwd=git_repo)

        assert current_branch(git_repo) == "develop"
        assert is_clean(git_repo)

    def test_checkout_succeeds_with_dirty_audit_files(self, git_repo):
        aah = _seed_tracked_aah_state(git_repo)
        _seed_diverged_branch(git_repo)

        (aah / "audit" / "token-usage.json").write_text('{"session_count": 42}\n')
        (aah / "audit" / "activity-log.jsonl").write_text('{"event": "stop"}\n')

        checkout_branch("develop", cwd=git_repo)

        assert current_branch(git_repo) == "develop"
        assert is_clean(git_repo)

    def test_guard_does_not_sweep_non_aah_changes(self, git_repo):
        """Only .aah/ is committed — user source edits stay in the working tree."""
        _seed_tracked_aah_state(git_repo)
        _seed_diverged_branch(git_repo)

        (git_repo / ".aah" / "claude-progress.json").write_text('{"current_wave": 9}\n')
        (git_repo / "app.py").write_text("print('work in progress')\n")

        checkout_branch("develop", cwd=git_repo)

        assert current_branch(git_repo) == "develop"
        assert (git_repo / "app.py").exists()
        status = run_git(["status", "--porcelain"], cwd=git_repo).stdout
        assert "app.py" in status
        assert "claude-progress.json" not in status

    def test_commit_state_false_skips_guard(self, git_repo):
        aah = _seed_tracked_aah_state(git_repo)
        _seed_diverged_branch(git_repo)
        (aah / "claude-progress.json").write_text('{"current_wave": 3}\n')

        with pytest.raises(GitError):
            checkout_branch("develop", cwd=git_repo, commit_state=False)

        assert current_branch(git_repo) == "main"

    def test_clean_tree_creates_no_commit(self, git_repo):
        _seed_tracked_aah_state(git_repo)
        _seed_diverged_branch(git_repo)
        before = len(get_log(git_repo, count=50))

        checkout_branch("develop", cwd=git_repo)

        # develop has its own diverge commit; assert the guard added none on top
        assert not any(
            "commit AAH state before branch switch" in e["subject"]
            for e in get_log(git_repo, count=50)
        )
        assert before > 0

    def test_checkout_create_with_dirty_state(self, git_repo):
        aah = _seed_tracked_aah_state(git_repo)
        (aah / "claude-progress.json").write_text('{"current_wave": 1}\n')

        checkout_branch("integration/wave-1", cwd=git_repo, create=True)

        assert current_branch(git_repo) == "integration/wave-1"
        assert is_clean(git_repo)

    def test_no_cwd_skips_guard(self, git_repo, monkeypatch):
        """cwd=None must not commit in whatever directory the process is in."""
        called = []
        monkeypatch.setattr(
            "aah.core.common.git_utils.commit_aah_state",
            lambda *a, **k: called.append(a),
        )
        monkeypatch.chdir(git_repo)
        create_branch("develop", cwd=git_repo)
        checkout_branch("develop")
        assert called == []


class TestIsLinkedWorktree:
    def test_main_repo_is_not_linked(self, git_repo):
        assert not is_linked_worktree(git_repo)

    def test_none_is_not_linked(self):
        assert not is_linked_worktree(None)

    def test_worktree_is_linked(self, git_repo):
        _seed_tracked_aah_state(git_repo)
        wt = git_repo / ".claude" / "worktrees" / "F001"
        run_git(["worktree", "add", str(wt), "-b", "feature/F001"], cwd=git_repo)
        assert is_linked_worktree(wt)

    def test_checkout_in_worktree_skips_guard(self, git_repo):
        """The blunt `git add .aah/` must not run inside a feature worktree."""
        _seed_tracked_aah_state(git_repo)
        create_branch("develop", cwd=git_repo)
        wt = git_repo / ".claude" / "worktrees" / "F001"
        run_git(["worktree", "add", str(wt), "-b", "feature/F001"], cwd=git_repo)
        _configure_git(wt)

        (wt / ".aah" / "claude-progress.json").write_text('{"current_wave": 7}\n')
        before = len(get_log(wt, count=50))

        # Guard skipped, so the dirty file is left alone and git itself decides.
        # Same-content checkout of a branch at the same commit is allowed by git.
        checkout_branch("feature/F001", cwd=wt)

        assert len(get_log(wt, count=50)) == before
        assert has_uncommitted_changes(wt)


class TestCommitAahState:
    def test_returns_false_on_clean_tree(self, git_repo):
        _seed_tracked_aah_state(git_repo)
        assert commit_aah_state(git_repo) is False

    def test_returns_false_when_only_non_aah_dirty(self, git_repo):
        _seed_tracked_aah_state(git_repo)
        (git_repo / "app.py").write_text("x = 1\n")
        assert commit_aah_state(git_repo) is False

    def test_commits_dirty_aah_state(self, git_repo):
        aah = _seed_tracked_aah_state(git_repo)
        (aah / "claude-progress.json").write_text('{"current_wave": 5}\n')
        assert commit_aah_state(git_repo) is True
        assert is_clean(git_repo)


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Invoke the git_utils CLI the way `aah run core.common.git_utils` does."""
    return subprocess.run(
        [sys.executable, "-m", "aah.core.common.git_utils", *args],
        cwd=cwd, capture_output=True, text=True, env=GIT_ENV,
    )


def _make_project(repo: Path) -> None:
    """Add the manifest that require_project_path looks for."""
    (repo / ".aah").mkdir(exist_ok=True)
    (repo / ".aah" / "manifest.yaml").write_text("project_name: test\n")


class TestCheckoutCLI:
    """`aah run core.common.git_utils checkout` — the guarded entry point for
    skill markdown, which runs raw bash and cannot call checkout_branch().

    Without this subcommand a skill author has no guarded command to reach for,
    which is why aah-build and aah-promote-to-main used bare `git checkout`.
    """

    def test_checkout_succeeds_with_dirty_state(self, git_repo):
        aah = _seed_tracked_aah_state(git_repo)
        # Commit the manifest before diverging, so it exists on both branches.
        _make_project(git_repo)
        add_all(cwd=git_repo)
        commit("Add manifest", cwd=git_repo)
        _seed_diverged_branch(git_repo)
        (aah / "claude-progress.json").write_text('{"current_wave": 3}\n')

        result = _run_cli(
            ["checkout", "develop", "--project-path", str(git_repo)], cwd=git_repo,
        )

        assert result.returncode == 0, result.stderr
        assert current_branch(git_repo) == "develop"
        assert result.stdout.strip() == "develop"

    def test_prints_resulting_branch(self, git_repo):
        _seed_tracked_aah_state(git_repo)
        _make_project(git_repo)
        add_all(cwd=git_repo)
        commit("Add manifest", cwd=git_repo)

        result = _run_cli(
            ["checkout", "integration/wave-1", "--create",
             "--project-path", str(git_repo)], cwd=git_repo,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "integration/wave-1"
        assert branch_exists("integration/wave-1", git_repo)

    def test_missing_project_exits_nonzero(self, tmp_path):
        """No .aah/manifest.yaml anywhere — must fail loudly, not switch."""
        result = _run_cli(["checkout", "develop", "--project-path", str(tmp_path)],
                          cwd=tmp_path)
        assert result.returncode != 0

    def test_nonexistent_branch_exits_1_not_traceback(self, git_repo):
        _seed_tracked_aah_state(git_repo)
        _make_project(git_repo)
        add_all(cwd=git_repo)
        commit("Add manifest", cwd=git_repo)

        result = _run_cli(
            ["checkout", "no-such-branch", "--project-path", str(git_repo)],
            cwd=git_repo,
        )

        assert result.returncode == 1
        assert "Traceback" not in result.stderr
        assert "Error:" in result.stderr


class TestSkillsUseGuardedCheckout:
    """Skill markdown must not reach for bare `git checkout`.

    These three sites failed with "your local changes would be overwritten"
    because raw bash bypasses checkout_branch() entirely.
    """

    SKILLS = Path(__file__).resolve().parents[1] / "aah" / "skills"

    def test_no_bare_git_checkout_in_skill_commands(self):
        offenders = []
        for skill in self.SKILLS.rglob("*.md"):
            for num, line in enumerate(skill.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                # Only flag executable lines; prose may discuss `git checkout`.
                if stripped.startswith("`") or stripped.startswith("-"):
                    continue
                if re.search(r"(^|&&\s*)git checkout\b", stripped):
                    offenders.append(f"{skill.name}:{num}: {stripped}")
        assert not offenders, "bare git checkout in skill markdown:\n" + "\n".join(offenders)

    def test_guarded_checkout_is_referenced(self):
        build = (self.SKILLS / "aah-build" / "SKILL.md").read_text(encoding="utf-8")
        promote = (self.SKILLS / "aah-promote-to-main" / "SKILL.md").read_text(encoding="utf-8")
        assert build.count("core.common.git_utils checkout") == 2
        assert "core.common.git_utils checkout main" in promote


def _configure_identity(repo: Path) -> None:
    run_git(["config", "user.email", "test@test.com"], cwd=repo)
    run_git(["config", "user.name", "Test"], cwd=repo)


def _staged(repo: Path) -> set[str]:
    result = run_git(["diff", "--cached", "--name-only"], cwd=repo, check=False)
    return {line for line in result.stdout.splitlines() if line.strip()}



class TestLog:
    def test_get_log(self, git_repo):
        entries = get_log(git_repo, count=5)
        assert len(entries) >= 1
        assert "hash" in entries[0]
        assert "subject" in entries[0]
        assert entries[0]["subject"] == "Initial commit"

    def test_date_is_strict_iso8601(self, git_repo):
        """%aI, not %ai — `datetime.fromisoformat` must parse it directly.

        Guards the session-cutoff comparison in generate_session_summary.
        """
        from datetime import datetime

        entries = get_log(git_repo, count=1)
        parsed = datetime.fromisoformat(entries[0]["date"])
        assert parsed.tzinfo is not None
        assert "T" in entries[0]["date"]

    def test_get_log_empty_repo(self, tmp_path):
        repo = tmp_path / "empty"
        repo.mkdir()
        init_repo(repo)
        entries = get_log(repo)
        assert entries == []
