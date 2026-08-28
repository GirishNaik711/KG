"""Shared test fixtures for AAH scripts tests."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def tmp_dir(tmp_path):
    """Return a temporary directory path."""
    return tmp_path


@pytest.fixture
def config_dir(tmp_path):
    """Create a temp directory with a aah-config.yaml."""
    config = {
        "workspace_root": str(tmp_path / "workspaces"),
        "active_workspace": None,
        "active_project": None,
        "settings": {
            "default_complexity_tier": "moderate",
            "git_branching": {
                "main_branch": "main",
                "develop_branch": "develop",
                "integration_prefix": "integration/wave-",
            },
        },
    }
    config_path = tmp_path / "aah-config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(config, f)
    return tmp_path, config_path


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repo with an initial commit."""
    repo_path = tmp_path / "test-repo"
    repo_path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_path, capture_output=True, check=True)

    # Create initial file and commit
    (repo_path / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "add", "-A"], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_path,
        capture_output=True,
        check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@test.com",
             "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@test.com"},
    )
    return repo_path


@pytest.fixture
def rapids_project(git_repo):
    """Create a git repo with .aah/ directory structure."""
    from aah.core.scaffold.rapids_dir import create_rapids_dir
    from aah.core.common.manifest import get_default_manifest, save_manifest
    from aah.core.common.progress import get_default_progress, save_progress

    aah_path = create_rapids_dir(git_repo)
    manifest = get_default_manifest("test-project", "greenfield")
    save_manifest(manifest, aah_path / "manifest.yaml")
    progress = get_default_progress()
    save_progress(progress, aah_path / "claude-progress.json")

    return git_repo


@pytest.fixture
def sample_features():
    """Return a list of sample feature definitions for testing."""
    return [
        {
            "id": "F001",
            "spec_ref": "SPEC-001",
            "description": "User authentication",
            "dependencies": [],
            "acceptance_criteria": ["Users can log in", "Users can log out"],
            "test_cases": [{"id": "TC001", "description": "Test login"}],
            "status": "pending",
        },
        {
            "id": "F002",
            "spec_ref": "SPEC-001",
            "description": "User registration",
            "dependencies": [],
            "acceptance_criteria": ["Users can register"],
            "test_cases": [{"id": "TC002", "description": "Test registration"}],
            "status": "pending",
        },
        {
            "id": "F003",
            "spec_ref": "SPEC-002",
            "description": "Dashboard",
            "dependencies": ["F001"],
            "acceptance_criteria": ["Dashboard shows user data"],
            "test_cases": [{"id": "TC003", "description": "Test dashboard"}],
            "status": "pending",
        },
        {
            "id": "F004",
            "spec_ref": "SPEC-002",
            "description": "User profile",
            "dependencies": ["F001", "F002"],
            "acceptance_criteria": ["Profile displays user info"],
            "test_cases": [{"id": "TC004", "description": "Test profile"}],
            "status": "pending",
        },
        {
            "id": "F005",
            "spec_ref": "SPEC-003",
            "description": "Admin panel",
            "dependencies": ["F003", "F004"],
            "acceptance_criteria": ["Admin can manage users"],
            "test_cases": [{"id": "TC005", "description": "Test admin panel"}],
            "status": "pending",
        },
    ]


@pytest.fixture
def sample_feature_list(sample_features):
    """Return a sample feature-list.json structure."""
    return {
        "features": [
            {
                "id": f["id"],
                "spec_ref": f["spec_ref"],
                "description": f["description"],
                "dependencies": f["dependencies"],
                "passes": False,
            }
            for f in sample_features
        ]
    }
