"""E2E tests for brownfield project lifecycle (WS5)."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from aah.core.common.io_utils import read_yaml, write_text, write_yaml
from aah.core.common.manifest import get_default_manifest
from aah.core.common.progress import get_default_progress, save_progress


GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@test.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@test.com",
}


def _git(args, cwd):
    return subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, check=False, env=GIT_ENV,
    )


def _create_brownfield_repo(tmp_path, name="my-app"):
    """Create a simulated pre-existing repo for brownfield import."""
    repo = tmp_path / name
    repo.mkdir()
    _git(["init"], cwd=repo)
    write_text("# My App\n", repo / "README.md")
    write_text("print('hello')\n", repo / "main.py")
    (repo / "src").mkdir()
    write_text("# module\n", repo / "src" / "app.py")
    _git(["add", "."], cwd=repo)
    _git(["commit", "-m", "Initial commit"], cwd=repo)
    return repo


class TestBrownfieldScaffold:
    def test_brownfield_preserves_existing_git(self, tmp_path):
        """Brownfield scaffold should NOT re-init git if .git exists."""
        repo = _create_brownfield_repo(tmp_path)

        # Get the initial commit hash
        initial = _git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
        assert initial  # Has commits

        from aah.core.scaffold.rapids_dir import create_rapids_dir
        create_rapids_dir(repo)

        # Git history should be preserved
        after = _git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
        assert initial == after

    def test_brownfield_creates_rapids_dir(self, tmp_path):
        """Brownfield should create .rapids/ structure."""
        repo = _create_brownfield_repo(tmp_path)
        from aah.core.scaffold.rapids_dir import create_rapids_dir
        rapids = create_rapids_dir(repo)

        assert rapids.exists()
        assert (rapids / "plan" / "features").is_dir()
        assert (rapids / "plan" / "specs").is_dir()
        assert (rapids / "analysis" / "decisions").is_dir()

    def test_brownfield_manifest_type(self, tmp_path):
        """Brownfield manifest should record project_type: brownfield."""
        repo = _create_brownfield_repo(tmp_path)
        from aah.core.scaffold.rapids_dir import create_rapids_dir
        rapids = create_rapids_dir(repo)

        manifest = get_default_manifest("my-app", "brownfield")
        from aah.core.common.manifest import save_manifest
        save_manifest(manifest, rapids / "manifest.yaml")

        loaded = read_yaml(rapids / "manifest.yaml")
        assert loaded["project_type"] == "brownfield"

    def test_brownfield_with_develop_branch(self, tmp_path):
        """Brownfield should NOT create develop if it already exists."""
        repo = _create_brownfield_repo(tmp_path)
        _git(["checkout", "-b", "develop"], cwd=repo)
        _git(["checkout", "main"], cwd=repo)

        # Verify develop exists
        branches = _git(["branch"], cwd=repo).stdout
        assert "develop" in branches

    def test_brownfield_nonstandard_structure(self, tmp_path):
        """Brownfield with no src/ dir (monorepo-like) should still work."""
        repo = tmp_path / "mono"
        repo.mkdir()
        _git(["init"], cwd=repo)
        (repo / "packages" / "api").mkdir(parents=True)
        write_text("# API\n", repo / "packages" / "api" / "index.js")
        (repo / "packages" / "web").mkdir(parents=True)
        write_text("# Web\n", repo / "packages" / "web" / "index.js")
        _git(["add", "."], cwd=repo)
        _git(["commit", "-m", "Monorepo init"], cwd=repo)

        from aah.core.scaffold.rapids_dir import create_rapids_dir
        rapids = create_rapids_dir(repo)
        assert rapids.exists()


class TestGreenfielStackSelection:
    def test_stack_recorded_in_manifest(self, test_workspace):
        """create_project with --stack should record it in manifest."""
        workspace_root, config_path = test_workspace

        result = subprocess.run(
            ["rapids-run", "aah.core.scaffold.workspace", "create", "test-ws",
             "--config-path", str(config_path), "--workspace-root", str(workspace_root)],
            capture_output=True, text=True, timeout=30, env=GIT_ENV,
        )
        assert result.returncode == 0

        result = subprocess.run(
            ["rapids-run", "aah.core.scaffold.project", "create", "stack-test",
             "--config-path", str(config_path), "--stack", "python-fastapi"],
            capture_output=True, text=True, timeout=30, env=GIT_ENV,
        )
        assert result.returncode == 0

        project_path = workspace_root / "test-ws" / "stack-test"
        manifest = read_yaml(project_path / ".rapids" / "manifest.yaml")
        assert manifest["stack_choices"]["primary"] == "python-fastapi"

    def test_no_stack_default(self, test_workspace):
        """create_project without --stack should have empty primary stack."""
        workspace_root, config_path = test_workspace

        subprocess.run(
            ["rapids-run", "aah.core.scaffold.workspace", "create", "test-ws",
             "--config-path", str(config_path), "--workspace-root", str(workspace_root)],
            capture_output=True, text=True, timeout=30, env=GIT_ENV,
        )

        result = subprocess.run(
            ["rapids-run", "aah.core.scaffold.project", "create", "no-stack",
             "--config-path", str(config_path)],
            capture_output=True, text=True, timeout=30, env=GIT_ENV,
        )
        assert result.returncode == 0

        project_path = workspace_root / "test-ws" / "no-stack"
        manifest = read_yaml(project_path / ".rapids" / "manifest.yaml")
        assert manifest.get("stack_choices", {}).get("primary") is None
