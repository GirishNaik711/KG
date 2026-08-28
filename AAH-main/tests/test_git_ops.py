"""Tests for aah.core.git_ops modules."""

import os
from pathlib import Path

import pytest

from aah.core.common.git_utils import (
    add_all,
    branch_exists,
    checkout_branch,
    commit,
    create_branch,
    current_branch,
    is_clean,
    rev_parse,
    run_git,
)
from aah.core.common.io_utils import write_json, write_yaml
from aah.core.common.manifest import get_default_manifest, save_manifest
from aah.core.git_ops.check_clean_git import check_clean_git
from aah.core.git_ops.merge_wave_to_integration import (
    _find_feature_branch,
    ensure_integration_branch,
    merge_feature_to_integration,
)
from aah.core.git_ops.setup_wave_worktrees import setup_wave_worktrees
from aah.core.git_ops.validate_main_merge import validate_main_merge


GIT_ENV_KEYS = ["GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"]


@pytest.fixture(autouse=True)
def git_env():
    """Set git env vars for tests."""
    os.environ["GIT_AUTHOR_NAME"] = "Test"
    os.environ["GIT_AUTHOR_EMAIL"] = "test@test.com"
    os.environ["GIT_COMMITTER_NAME"] = "Test"
    os.environ["GIT_COMMITTER_EMAIL"] = "test@test.com"
    yield
    for key in GIT_ENV_KEYS:
        os.environ.pop(key, None)


class TestCheckCleanGit:
    def test_clean_repo(self, git_repo):
        is_clean_result, msg = check_clean_git(git_repo)
        assert is_clean_result
        assert "clean" in msg.lower()

    def test_dirty_repo(self, git_repo):
        (git_repo / "new_file.txt").write_text("uncommitted")
        is_clean_result, msg = check_clean_git(git_repo)
        assert not is_clean_result
        assert "uncommitted" in msg.lower()

    def test_non_git_dir(self, tmp_path):
        is_clean_result, msg = check_clean_git(tmp_path)
        assert is_clean_result  # Should not block non-git directories


class TestValidateMainMerge:
    def _setup_project(self, git_repo):
        aah_root = git_repo / ".aah"
        for d in ["plan/features", "build/test-results"]:
            (aah_root / d).mkdir(parents=True)
        manifest = get_default_manifest("test")
        manifest["current_phase"] = "build"
        save_manifest(manifest, aah_root / "manifest.yaml")
        return git_repo

    def test_not_ready_no_feature_list(self, git_repo):
        proj = self._setup_project(git_repo)
        result = validate_main_merge(proj)
        assert not result["ready"]

    def test_not_ready_failing_features(self, git_repo):
        proj = self._setup_project(git_repo)
        aah_root = proj / ".aah"
        write_json({
            "features": [
                {"id": "F001", "passes": True},
                {"id": "F002", "passes": False},
            ]
        }, aah_root / "feature-list.json")

        result = validate_main_merge(proj)
        assert not result["ready"]
        assert any("not passing" in i.lower() or "feature" in i.lower() for i in result["issues"])

    def test_not_ready_no_regression(self, git_repo):
        proj = self._setup_project(git_repo)
        aah_root = proj / ".aah"
        write_json({
            "features": [{"id": "F001", "passes": True}]
        }, aah_root / "feature-list.json")

        result = validate_main_merge(proj)
        assert not result["ready"]
        assert any("regression" in i.lower() for i in result["issues"])

    def test_ready_all_checks_pass(self, git_repo):
        proj = self._setup_project(git_repo)
        aah_root = proj / ".aah"

        write_json({
            "features": [{"id": "F001", "passes": True}]
        }, aah_root / "feature-list.json")

        write_json({"passed": True, "total_tests": 5, "failed_count": 0},
                   aah_root / "build" / "test-results" / "regression-latest.json")

        from aah.core.common.progress import save_progress, get_default_progress
        p = get_default_progress()
        p["current_phase"] = "build"
        p["in_progress_features"] = []
        p["known_issues"] = []
        save_progress(p, aah_root / "claude-progress.json")

        result = validate_main_merge(proj)
        assert result["ready"]


@pytest.fixture
def git_repo_with_develop(git_repo):
    """Git repo with both main and develop branches."""
    create_branch("develop", cwd=git_repo)
    checkout_branch("main", cwd=git_repo)
    return git_repo


def _setup_rapids_project(repo_path):
    """Create minimal .aah structure for merge tests."""
    aah_root = repo_path / ".aah"
    for d in ["plan", "build"]:
        (aah_root / d).mkdir(parents=True, exist_ok=True)
    write_yaml({
        "project_name": "test",
        "branching_config": {
            "develop_branch": "develop",
            "integration_prefix": "integration/wave-",
        },
    }, aah_root / "manifest.yaml")
    write_json({"waves": [["F001", "F002"], ["F003"]], "total_waves": 2}, aah_root / "plan" / "waves.json")
    add_all(cwd=repo_path)
    commit("chore: add aah_root structure", cwd=repo_path)
    return aah_root


class TestEnsureIntegrationBranch:
    def test_creates_from_develop(self, git_repo_with_develop):
        repo = git_repo_with_develop
        _setup_rapids_project(repo)
        result = ensure_integration_branch(repo, 0)
        assert result["created"] is True
        assert result["branch"] == "integration/wave-0"
        assert branch_exists("integration/wave-0", cwd=repo)

    def test_idempotent(self, git_repo_with_develop):
        repo = git_repo_with_develop
        _setup_rapids_project(repo)
        result1 = ensure_integration_branch(repo, 0)
        assert result1["created"] is True
        result2 = ensure_integration_branch(repo, 0)
        assert result2["created"] is False
        assert result2["branch"] == "integration/wave-0"


class TestMergeFeatureToIntegration:
    def test_merge_single_feature(self, git_repo_with_develop):
        repo = git_repo_with_develop
        _setup_rapids_project(repo)

        # Create integration branch
        ensure_integration_branch(repo, 0)

        # Create a feature branch with a commit
        checkout_branch("develop", cwd=repo)
        create_branch("feature/F001", cwd=repo)
        checkout_branch("feature/F001", cwd=repo)
        (repo / "feature_f001.py").write_text("# F001 implementation\n")
        add_all(cwd=repo)
        commit("feat(F001): implement auth", cwd=repo)
        checkout_branch("develop", cwd=repo)

        result = merge_feature_to_integration(repo, 0, "F001")
        assert result["success"] is True
        assert result["merged"] == "F001"
        assert result["conflicts"] == []

    def test_merge_conflict_detected(self, git_repo_with_develop):
        repo = git_repo_with_develop
        _setup_rapids_project(repo)

        ensure_integration_branch(repo, 0)

        # Feature F001: modify a file
        checkout_branch("develop", cwd=repo)
        create_branch("feature/F001", cwd=repo)
        checkout_branch("feature/F001", cwd=repo)
        (repo / "shared.py").write_text("# F001 version\n")
        add_all(cwd=repo)
        commit("feat(F001): add shared", cwd=repo)
        checkout_branch("develop", cwd=repo)

        # Merge F001 first (should succeed)
        merge_feature_to_integration(repo, 0, "F001")

        # Feature F002: modify the same file differently, branched from develop
        create_branch("feature/F002", cwd=repo)
        checkout_branch("feature/F002", cwd=repo)
        (repo / "shared.py").write_text("# F002 conflicting version\n")
        add_all(cwd=repo)
        commit("feat(F002): add shared conflict", cwd=repo)
        checkout_branch("develop", cwd=repo)

        # Merge F002 should detect conflict
        result = merge_feature_to_integration(repo, 0, "F002")
        assert result["success"] is False
        assert len(result["conflicts"]) > 0
        assert result["conflicts"][0]["feature"] == "F002"


class TestFindFeatureBranchEnhanced:
    def test_worktree_pattern_discovery(self, git_repo_with_develop):
        repo = git_repo_with_develop
        aah_root = _setup_aah_root_project(repo)

        # Create a worktree-style branch with a feature commit
        checkout_branch("develop", cwd=repo)
        create_branch("worktree-agent-abc123", cwd=repo)
        checkout_branch("worktree-agent-abc123", cwd=repo)
        (repo / "impl.py").write_text("# F005 work\n")
        add_all(cwd=repo)
        commit("feat(F005): implement feature", cwd=repo)
        checkout_branch("develop", cwd=repo)

        # Should find the worktree branch via commit grep
        found = _find_feature_branch(repo, aah_root, "F005")
        assert found == "worktree-agent-abc123"

    def test_manifest_fallback(self, git_repo_with_develop):
        repo = git_repo_with_develop
        aah_root = _setup_aah_root_project(repo)

        # Create a branch with a non-standard name and commit
        checkout_branch("develop", cwd=repo)
        create_branch("custom-branch-xyz", cwd=repo)
        checkout_branch("custom-branch-xyz", cwd=repo)
        (repo / "f010.py").write_text("# F010\n")
        add_all(cwd=repo)
        commit("implement F010", cwd=repo)
        # Get the SHA
        sha_result = run_git(["rev-parse", "HEAD"], cwd=repo)
        sha = sha_result.stdout.strip()
        checkout_branch("develop", cwd=repo)

        # Write feature-commits.json with that SHA
        (aah_root / "build").mkdir(parents=True, exist_ok=True)
        write_json({"F010": [sha]}, aah_root / "build" / "feature-commits.json")

        found = _find_feature_branch(repo, aah_root, "F010")
        assert found == "custom-branch-xyz"


class TestSetupWaveWorktrees:
    """setup_wave_worktrees must return a full descriptor
    (path + branch + created + sha) per feature, not a path-only map.

    Uses real temp git repos and real ``git worktree add`` — NO MOCKS.
    """

    def test_descriptor_shape_includes_sha(self, git_repo_with_develop):
        repo = git_repo_with_develop
        _setup_rapids_project(repo)

        result = setup_wave_worktrees(repo, 0, ["F001"])
        assert result["success"] is True
        desc = result["worktrees"]["F001"]
        assert set(desc.keys()) == {"path", "branch", "created", "sha"}
        assert desc["branch"] == "feature/F001"
        assert desc["created"] is True
        assert len(desc["sha"]) == 40
        assert all(c in "0123456789abcdef" for c in desc["sha"])

    def test_sha_equals_worktree_head(self, git_repo_with_develop):
        repo = git_repo_with_develop
        _setup_rapids_project(repo)

        result = setup_wave_worktrees(repo, 0, ["F001"])
        desc = result["worktrees"]["F001"]
        head = rev_parse("HEAD", cwd=Path(desc["path"]))
        assert head == desc["sha"]

    def test_idempotent_rerun_preserves_sha(self, git_repo_with_develop):
        repo = git_repo_with_develop
        _setup_rapids_project(repo)

        first = setup_wave_worktrees(repo, 0, ["F001"])
        first_desc = first["worktrees"]["F001"]
        assert first_desc["created"] is True

        second = setup_wave_worktrees(repo, 0, ["F001"])
        second_desc = second["worktrees"]["F001"]
        # Re-run skips existing worktree but still resolves + returns the SHA.
        assert second_desc["created"] is False
        assert second_desc["sha"] == first_desc["sha"]
        assert len(second_desc["sha"]) == 40

    def test_success_true_and_no_errors(self, git_repo_with_develop):
        repo = git_repo_with_develop
        _setup_rapids_project(repo)

        result = setup_wave_worktrees(repo, 0, ["F001", "F002"])
        assert result["success"] is True
        assert result["errors"] == []
        assert set(result["worktrees"].keys()) == {"F001", "F002"}
        for desc in result["worktrees"].values():
            assert len(desc["sha"]) == 40
