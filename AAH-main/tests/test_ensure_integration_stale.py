"""ensure_integration_branch refreshes a stale integration branch (bug #17).

A leftover integration/wave-N cut from an OLD develop (missing later develop
commits) must be refreshed to contain current develop HEAD, not blindly reused.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aah.core.common.git_utils import is_ancestor, rev_parse
from aah.core.git_ops.merge_wave_to_integration import ensure_integration_branch


def _git(cwd: Path, *args: str):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "develop")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "skeleton.py").write_text("v0\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "wave-0 skeleton")
    return tmp_path


def test_stale_integration_is_refreshed(repo: Path):
    # integration/wave-1 created from OLD develop (before the dep commit).
    _git(repo, "branch", "integration/wave-1")

    # develop advances with the dependency the wave needs.
    (repo / "dep.py").write_text("needed by wave 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "wave-0 promote: add dependency")
    develop_head = rev_parse("develop", cwd=repo)

    # Stale branch does NOT yet contain develop HEAD.
    assert not is_ancestor(develop_head, "integration/wave-1", cwd=repo)

    result = ensure_integration_branch(repo, 1)

    assert result["created"] is False
    assert result.get("refreshed") is True
    assert result.get("stale") is False
    assert is_ancestor(develop_head, "integration/wave-1", cwd=repo), \
        "integration branch still stale after ensure"


def test_current_integration_reused_as_is(repo: Path):
    # integration/wave-1 already at develop HEAD → plain reuse, no refresh churn.
    _git(repo, "branch", "integration/wave-1")
    result = ensure_integration_branch(repo, 1)
    assert result == {"created": False, "branch": "integration/wave-1"}
