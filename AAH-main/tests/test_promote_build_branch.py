"""Isolated integration tests for the lean, wave-free build-branch promote.

Verifies ``promote_build_to_develop`` + ``verify_build_evidence``:
  - promotes the single build branch to develop when BOTH the standards gate
    and the cumulative regression evidence are attested + passing + bound to
    the build tip,
  - BLOCKS when either is missing, stale, or reports failure,
  - requires NO runtime evidence (advisory, cleared at the module checkpoint).

NO MOCKS: real throwaway Git repos under pytest ``tmp_path`` (AAHProjectBuilder)
plus a real local ``--bare`` remote. Attested evidence is written with the real
producer writer. The harness repo is never touched — every git op targets the
fixture via ``project_path``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from aah.core.build.evidence import write_attested_result
from aah.core.build.quality_checks import build_standards_artifact
from aah.core.build.verification_evidence import (
    FEATURE_TEST_PREFIX,
    QUALITY_PREFIX,
    REGRESSION_PREFIX,
)
from aah.core.build.verification_identity import subject_identity
from aah.core.common.io_utils import read_json, write_json
from aah.core.git_ops.promote_to_develop import promote_build_to_develop
from tests.support.aah_project import AAHProjectBuilder


def _seed(
    tmp_path: Path,
    *,
    regression_status: str = "pass",
    standards_status: str = "pass",
) -> AAHProjectBuilder:
    """Build an isolated repo: develop + build/p (a feature commit) + a bare
    remote + attested standards and regression evidence bound to build/p's code
    identity."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)

    b = AAHProjectBuilder.create(tmp_path, git=True, branch="develop")
    # The attestation secret is 32 random BINARY bytes. With the machine's global
    # core.autocrlf=true, git would CRLF-normalize it on checkout and corrupt it,
    # so pin the fixture repo to leave bytes untouched.
    b.git("config", "core.autocrlf", "false")
    b.git("config", "core.safecrlf", "false")
    b.manifest(project_name="p").secret()
    b.file("app.py", "x = 1\n").commit("base on develop")
    b.git("remote", "add", "origin", str(remote))
    b.git("push", "-u", "origin", "develop")

    # Single shared build branch with one feature's commit.
    b.git("checkout", "-b", "build/p")
    b.file("feature_a.py", "# F-MOD-000 implementation\n").commit("feat(F-MOD-000): add A")
    write_json({"features": [{"id": "F-MOD-000", "passes": False}]}, b.aah / "feature-list.json")

    sha = subject_identity(b.path, ref="build/p")

    # Attested per-feature evidence (what the implementer's Step 7 official run
    # produces) — required by update_feature_status before marking a feature passing.
    write_attested_result(
        {"feature_id": "F-MOD-000", "status": "pass", "passed": True,
         "subject": {"branch": "build/p", "commit_sha": sha}},
        b.aah / "build" / "test-results" / "F-MOD-000.json",
        project_path=b.path,
        command=FEATURE_TEST_PREFIX,
        artifact_name="feature test results",
    )

    # Attested cumulative regression evidence bound to build/p's CODE identity
    # (excludes .aah) — the lean build's single promotion gate.
    write_attested_result(
        {"status": regression_status, "passed": regression_status == "pass",
         "subject": {"branch": "build/p", "commit_sha": sha}},
        b.aah / "build" / "test-results" / "regression-latest.json",
        project_path=b.path,
        command=REGRESSION_PREFIX,
        artifact_name="regression results",
    )

    # Attested whole-codebase standards evidence — the lean build's other
    # promotion gate, run before regression.
    write_attested_result(
        {"scope": "project", "schema_version": 1, "status": standards_status,
         "overall_passed": standards_status == "pass",
         "verdict": "PASS" if standards_status == "pass" else "BLOCK",
         "scopes": [{"stack": "python", "root": "."}],
         "subject": {"branch": "build/p", "commit_sha": sha}},
        build_standards_artifact(b.aah),
        project_path=b.path,
        command=QUALITY_PREFIX,
        artifact_name="project standards check",
    )
    return b


def test_promote_build_branch_success(tmp_path):
    b = _seed(tmp_path, regression_status="pass")
    develop_before = b.git("rev-parse", "develop")

    result = promote_build_to_develop(b.path, "build/p")
    assert result["success"], result

    # develop advanced and now contains the build branch tip.
    assert b.git("rev-parse", "develop") != develop_before
    contains = subprocess.run(
        ["git", "merge-base", "--is-ancestor", result["build_head"], "develop"],
        cwd=b.path, capture_output=True,
    )
    assert contains.returncode == 0, "develop does not contain the build tip"

    # feature-list marked passing.
    fl = read_json(b.aah / "feature-list.json")
    assert all(f.get("passes") is True for f in fl["features"])


def test_promote_blocks_on_missing_regression(tmp_path):
    b = _seed(tmp_path, regression_status="pass")
    (b.aah / "build" / "test-results" / "regression-latest.json").unlink()
    develop_before = b.git("rev-parse", "develop")

    result = promote_build_to_develop(b.path, "build/p")
    assert result["success"] is False
    assert result["verification_failures"][0]["code"] == "regression_missing"
    # develop was NOT moved.
    assert b.git("rev-parse", "develop") == develop_before


def test_promote_blocks_on_failed_regression(tmp_path):
    b = _seed(tmp_path, regression_status="fail")
    develop_before = b.git("rev-parse", "develop")

    result = promote_build_to_develop(b.path, "build/p")
    assert result["success"] is False
    assert result["verification_failures"][0]["code"] == "regression_failed"
    assert b.git("rev-parse", "develop") == develop_before


def test_promote_ignores_standards_evidence(tmp_path):
    """Promote no longer verifies standards — the build skill's gate loop owns it.

    A failing standards artifact, or none at all, does not block promotion: the
    standards gate enforces itself via its exit code (fail -> aah-fix -> re-run),
    and on a single-branch build the code is already on develop before promote
    runs, so re-checking here prevented nothing.
    """
    b = _seed(tmp_path, standards_status="fail")
    build_standards_artifact(b.aah).unlink()
    develop_before = b.git("rev-parse", "develop")

    result = promote_build_to_develop(b.path, "build/p")
    assert result["success"] is True, result.get("error")
    assert b.git("rev-parse", "develop") != develop_before


def test_promote_blocks_on_stale_subject_via_regression(tmp_path):
    """A repair commit after the gates ran moves the tree — regression catches it."""
    b = _seed(tmp_path)
    b.file("late_fix.py", "# lint repair\n").commit("fix: lint")
    develop_before = b.git("rev-parse", "develop")

    result = promote_build_to_develop(b.path, "build/p")
    assert result["success"] is False
    assert result["verification_failures"][0]["code"] == "subject_sha_stale"
    assert b.git("rev-parse", "develop") == develop_before
