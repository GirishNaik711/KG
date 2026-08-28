"""Tests for aah.core.guards.verification_artifact_guard.

Phase 2B: BLOCK. Warning-path tests (verification path + no env var)
assert exit code 2 (block); suppression-path tests (env var set,
non-verification path, delegated mode) stay at exit code 0 since
those cases legitimately allow the write.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def isolated_cwd():
    """A tmp dir with no aah-config.yaml or .aah/manifest.yaml above it.

    Some test environments have an active workspace whose manifest is
    marked ``execution_mode: delegated``; running the guard from a cwd
    inside that tree triggers ``exit_if_delegated()`` and silently
    suppresses the warn path we're testing. Use this fixture as the
    subprocess cwd to guarantee a clean lookup.
    """
    with tempfile.TemporaryDirectory(prefix="vag-isolated-") as td:
        yield Path(td)


def _run_guard(
    hook_input: dict,
    env: dict | None = None,
    cwd: Path | None = None,
) -> tuple[int, str]:
    proc_env = {**os.environ}
    # Strip any inherited active-project hints (AAH_CONTEXT, if set,
    # would override any cwd-based lookup and reintroduce the
    # delegated-workspace problem).
    proc_env.pop("AAH_CONTEXT", None)
    if env:
        proc_env.update(env)
    # Default cwd: tmp dir with no config above it. Tests that need a
    # specific cwd (e.g. delegated-mode test) override this.
    if cwd is None:
        # Use a tmp dir created on the fly (one per call). Cheap and
        # self-cleaning when the test finishes.
        with tempfile.TemporaryDirectory(prefix="vag-call-") as td:
            proc = subprocess.run(
                [sys.executable, "-m", "aah.core.guards.verification_artifact_guard"],
                input=json.dumps(hook_input),
                capture_output=True,
                text=True,
                cwd=td,
                env=proc_env,
            )
            return proc.returncode, proc.stderr
    proc = subprocess.run(
        [sys.executable, "-m", "aah.core.guards.verification_artifact_guard"],
        input=json.dumps(hook_input),
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=proc_env,
    )
    return proc.returncode, proc.stderr


class TestWarningPath:
    def test_warns_on_test_results_write_without_env(self):
        code, stderr = _run_guard(
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "/x/proj/.aah/build/test-results/F001.json"
                },
            },
        )
        assert code == 2
        assert "outside attestation" in stderr

    def test_warns_on_runtime_results(self):
        code, stderr = _run_guard(
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "/x/proj/.aah/build/runtime-results/wave-0-all.json"
                },
            },
        )
        assert code == 2
        assert "outside attestation" in stderr

    def test_warns_on_quality_results(self):
        code, stderr = _run_guard(
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "/x/proj/.aah/build/quality-results/F001-static-analysis.json"
                },
            },
        )
        assert code == 2
        assert "outside attestation" in stderr

    def test_warns_on_checkpoint_results(self):
        code, stderr = _run_guard(
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "/x/proj/.aah/build/checkpoint-results/wave-0-system-checkpoint.json"
                },
            },
        )
        assert code == 2
        assert "outside attestation" in stderr

    def test_warns_on_validation_results(self):
        code, stderr = _run_guard(
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "/x/proj/.aah/build/validation-results/F001-spec-validation.json"
                },
            },
        )
        assert code == 2
        assert "outside attestation" in stderr

    def test_warns_on_edit_too(self):
        code, stderr = _run_guard(
            {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "/x/proj/.aah/build/test-results/F001.json"
                },
            },
        )
        assert code == 2
        assert "outside attestation" in stderr

    def test_warns_on_multiedit_top_level_path(self):
        code, stderr = _run_guard(
            {
                "tool_name": "MultiEdit",
                "tool_input": {
                    "file_path": "/x/proj/.aah/build/test-results/F001.json",
                    "edits": [],
                },
            },
        )
        assert code == 2
        assert "outside attestation" in stderr

    def test_warns_on_multiedit_per_edit_path(self):
        code, stderr = _run_guard(
            {
                "tool_name": "MultiEdit",
                "tool_input": {
                    "edits": [
                        {"file_path": "/x/proj/.aah/build/test-results/F001.json"}
                    ],
                },
            },
        )
        assert code == 2
        assert "outside attestation" in stderr

    def test_warns_with_windows_style_path(self):
        code, stderr = _run_guard(
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": r"C:\proj\.aah\implement\test-results\F001.json"
                },
            },
        )
        assert code == 2
        assert "outside attestation" in stderr


class TestSuppressionPaths:
    def test_silent_when_env_var_set(self):
        code, stderr = _run_guard(
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "/x/proj/.aah/build/test-results/F001.json"
                },
            },
            env={"AAH_VERIFICATION_WRITE": "1"},
        )
        assert code == 0
        assert "outside attestation" not in stderr

    def test_env_var_with_other_value_does_not_suppress(self):
        code, stderr = _run_guard(
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "/x/proj/.aah/build/test-results/F001.json"
                },
            },
            env={"AAH_VERIFICATION_WRITE": "0"},
        )
        # Only the literal "1" suppresses.
        assert code == 2
        assert "outside attestation" in stderr

    def test_silent_on_non_verification_path(self):
        code, stderr = _run_guard(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "/x/proj/src/main.py"},
            },
        )
        assert code == 0
        # The warn message we'd emit must NOT be present. Stderr from
        # nested utilities (e.g. "manifest.yaml not found" from a no-project
        # cwd) is fine — that's environmental, not from this guard.
        assert "outside attestation" not in stderr

    def test_silent_on_other_aah_paths(self):
        # Plan/discuss/etc. are NOT verification artifacts.
        for path in (
            "/x/proj/.aah/plan/features/F001.yaml",
            "/x/proj/.aah/manifest.yaml",
            "/x/proj/.aah/audit/log.json",
        ):
            code, stderr = _run_guard(
                {"tool_name": "Write", "tool_input": {"file_path": path}},
            )
            assert code == 0
            assert "outside attestation" not in stderr, (
                f"unexpected warn for {path}: {stderr!r}"
            )

    def test_silent_on_unknown_tool(self):
        code, stderr = _run_guard(
            {
                "tool_name": "Glob",
                "tool_input": {"pattern": "**/test-results/*.json"},
            },
        )
        assert code == 0
        assert "outside attestation" not in stderr

    def test_silent_on_empty_input(self):
        with tempfile.TemporaryDirectory(prefix="vag-empty-") as td:
            proc_env = {**os.environ}
            proc_env.pop("AAH_CONTEXT", None)
            proc = subprocess.run(
                [sys.executable, "-m", "aah.core.guards.verification_artifact_guard"],
                input="",
                capture_output=True,
                text=True,
                cwd=td,
                env=proc_env,
            )
            assert proc.returncode == 0

    def test_silent_on_malformed_json(self):
        with tempfile.TemporaryDirectory(prefix="vag-malformed-") as td:
            proc_env = {**os.environ}
            proc_env.pop("AAH_CONTEXT", None)
            proc = subprocess.run(
                [sys.executable, "-m", "aah.core.guards.verification_artifact_guard"],
                input="not json",
                capture_output=True,
                text=True,
                cwd=td,
                env=proc_env,
            )
            assert proc.returncode == 0


class TestDelegatedMode:
    def test_silent_when_delegated(self, rapids_project):
        from aah.core.common.manifest import load_manifest, save_manifest
        manifest_path = rapids_project / ".aah" / "manifest.yaml"
        manifest = load_manifest(manifest_path)
        manifest["execution_mode"] = "delegated"
        save_manifest(manifest, manifest_path)

        # Run the guard from inside the delegated project's cwd so
        # find_manifest() picks up our edited manifest.
        proc = subprocess.run(
            [sys.executable, "-m", "aah.core.guards.verification_artifact_guard"],
            input=json.dumps({
                "tool_name": "Write",
                "tool_input": {
                    "file_path": str(rapids_project / ".aah/build/test-results/F001.json")
                },
            }),
            capture_output=True,
            text=True,
            cwd=str(rapids_project),
        )
        assert proc.returncode == 0
        # exit_if_delegated should fire BEFORE the warn path, so no stderr.
        assert "outside attestation" not in proc.stderr
