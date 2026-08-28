"""Regression-suite branch-gate tests for the lean/sequential (no-wave) model.

Verifies the v2 change to ``run_regression_suite``:
  - ``--subject-branch`` asserts the run happened on the single build branch,
  - ``--wave`` is now OPTIONAL (legacy integration/wave-N assertion still works),
  - with NEITHER, there is no branch assertion at all.

NO MOCKS: real Git-backed throwaway projects built under pytest ``tmp_path`` via
AAHProjectBuilder. Every assertion targets an early-return gate that fires BEFORE
``ensure_test_environment`` (wrong-branch and tracked-dirt), so no Docker/services
run and the harness repo is never touched — the subject is always the fixture repo.
"""

from __future__ import annotations

from pathlib import Path

from aah.core.build.run_regression_suite import run_regression_suite
from tests.support.aah_project import AAHProjectBuilder


def _project(tmp_path: Path, branch: str) -> AAHProjectBuilder:
    """A committed, clean, Git-backed fixture project on ``branch``."""
    builder = AAHProjectBuilder.create(tmp_path, git=True, branch=branch)
    # Global core.autocrlf=true would corrupt the 32-byte binary attestation
    # secret on checkout; pin the fixture repo to leave bytes untouched.
    builder.git("config", "core.autocrlf", "false")
    builder.git("config", "core.safecrlf", "false")
    builder.manifest(project_name="p").secret()
    builder.file("app.py", "x = 1\n").commit("seed")
    return builder


def test_wrong_subject_branch_skips(tmp_path):
    b = _project(tmp_path, "build/p")
    result = run_regression_suite(b.path, subject_branch="not-the-build-branch")
    assert result["status"] == "skipped_wrong_branch"
    assert result["expected_branch"] == "not-the-build-branch"


def test_legacy_wave_mismatch_still_skips(tmp_path):
    b = _project(tmp_path, "build/p")
    result = run_regression_suite(b.path, wave=999)
    assert result["status"] == "skipped_wrong_branch"
    assert result["expected_branch"] == "integration/wave-999"


def test_matching_subject_branch_passes_gate(tmp_path):
    # On the build branch, a tracked change outside .aah/ stops the run at the
    # tree-dirty gate — which proves the branch gate PASSED (no wrong-branch skip)
    # while still returning before any infra setup.
    b = _project(tmp_path, "build/p")
    (b.path / "app.py").write_text("x = 2\n")  # tracked dirt, outside .aah/
    result = run_regression_suite(b.path, subject_branch="build/p")
    assert result["status"] == "no_signal"
    assert result.get("signal_reason") == "tree_dirty_before_regression"


def test_no_branch_arg_no_assertion(tmp_path):
    # Neither --wave nor --subject-branch: no branch assertion at all. The run
    # gets past the (absent) branch gate to the tree-dirty gate regardless of
    # what branch we are on — proving --wave is now optional.
    b = _project(tmp_path, "some/other-branch")
    (b.path / "app.py").write_text("x = 3\n")  # tracked dirt
    result = run_regression_suite(b.path)
    assert result["status"] == "no_signal"
    assert result.get("signal_reason") == "tree_dirty_before_regression"
