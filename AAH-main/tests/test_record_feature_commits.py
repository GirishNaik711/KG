"""record_feature_commits attributes commits by feature branch, not message grep.

Regression guard for the mis-attribution deadlock (weather-cli bugs #6/#15): a
develop-side chore commit that merely MENTIONS the feature id must NOT be
recorded, while a genuine feat(<id>): commit on the feature branch MUST be.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from aah.core.build.record_feature_commits import record_feature_commits


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "develop")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "seed.txt").write_text("seed\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "chore: seed")
    return tmp_path


def test_branch_commit_recorded_chore_mention_ignored(repo: Path):
    fid = "F-MOD000-00"

    # A develop-side chore commit that merely mentions the feature id.
    (repo / "state.txt").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m",
         f"chore: auto-commit AAH framework state files (session end) {fid}")
    chore_sha = _git(repo, "rev-parse", "HEAD")

    # A genuine feature commit on the feature branch.
    _git(repo, "checkout", "-b", f"feature/{fid}")
    (repo / "feat.py").write_text("print('hi')\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", f"feat({fid}): implement the thing")
    feat_sha = _git(repo, "rev-parse", "HEAD")

    data = record_feature_commits(repo, fid)
    recorded = set(data[fid])

    assert feat_sha in recorded, "genuine feat(<id>) commit must be recorded"
    assert chore_sha not in recorded, "unrelated chore mention must NOT be recorded"

    # Manifest persisted the same result.
    manifest = json.loads((repo / ".aah" / "build" / "feature-commits.json").read_text())
    assert chore_sha not in manifest[fid]
