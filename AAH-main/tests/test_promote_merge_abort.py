"""Promotion never merges on unverified or drifted evidence, and never leaves a
half-merged tree.

- A source conflict aborts cleanly (no MERGE_HEAD) and reports failure.
- A .aah/-state-only conflict resolves integration-side and completes.
- Dirt outside AAH_STATE_PATHS, a verification failure, or a changed report
  snapshot each leave the current branch unchanged and prevent the merge.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.support.aah_project import AAHProjectBuilder

from aah.core.git_ops.promote_to_develop import (
    _merge_with_aah_resolution,
    promote_to_develop,
)


def _git(cwd: Path, *args: str):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _has_merge_head(repo: Path) -> bool:
    r = subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                       cwd=repo, capture_output=True, text=True)
    return r.returncode == 0


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "develop")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "app.py").write_text("base\n")
    (tmp_path / ".aah").mkdir()
    (tmp_path / ".aah" / "progress.json").write_text('{"wave": 0}\n')
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "checkout", "-b", "integration/wave-1")
    return tmp_path


def test_source_conflict_aborts_clean(repo: Path):
    # Diverge app.py on both branches → real source conflict.
    (repo / "app.py").write_text("integration change\n")
    _git(repo, "commit", "-am", "int: app change")
    _git(repo, "checkout", "develop")
    (repo / "app.py").write_text("develop change\n")
    _git(repo, "commit", "-am", "dev: app change")

    out = _merge_with_aah_resolution(repo, "integration/wave-1", "develop")

    assert out["success"] is False
    assert not _has_merge_head(repo), "tree left half-merged after source conflict"
    # working tree clean
    st = _git(repo, "status", "--porcelain").stdout.strip()
    assert st == "", f"tree not clean after abort: {st!r}"


def test_aah_state_conflict_resolves_integration_side(repo: Path):
    # Diverge ONLY .aah/ state → resolved integration-side, merge completes.
    (repo / ".aah" / "progress.json").write_text('{"wave": 1, "src": "integration"}\n')
    _git(repo, "commit", "-am", "int: wave-1 state")
    _git(repo, "checkout", "develop")
    (repo / ".aah" / "progress.json").write_text('{"wave": 0, "src": "develop"}\n')
    _git(repo, "commit", "-am", "dev: state")

    out = _merge_with_aah_resolution(repo, "integration/wave-1", "develop")

    assert out["success"] is True
    assert not _has_merge_head(repo)
    # integration side won
    assert "integration" in (repo / ".aah" / "progress.json").read_text()


WAVE = 1
BRANCH = f"integration/wave-{WAVE}"


@pytest.fixture
def promote_project(tmp_path: Path) -> Path:
    """A real project on integration/wave-1 with a develop branch to promote to.

    Deliberately WITHOUT complete wave evidence: every test below asserts that
    promotion refuses, and the branch it refuses on must not change.
    """
    import json as _json
    import yaml

    builder = (
        AAHProjectBuilder.create(tmp_path, name="promote-proj", branch="develop")
        .dirs("build/test-results", "build/runtime-results", "build/checkpoint-results")
        .secret()
        .manifest(project_name="promote-proj")
        .waves([[], ["F001"]])
        .feature("F001")
        .file("src/app.py", "print('hi')\n")
    )
    (builder.aah / "plan" / "checkpoint-config.yaml").write_text(
        yaml.safe_dump({"checkpoint_configuration": {"system_checkpoints": {}}}),
        encoding="utf-8",
    )
    (builder.aah / "feature-list.json").write_text(
        _json.dumps({"features": [{"id": "F001", "passes": True}]}), encoding="utf-8",
    )
    builder.commit()
    builder.git("branch", BRANCH)
    builder.git("checkout", BRANCH)
    return builder.path


def _current(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
