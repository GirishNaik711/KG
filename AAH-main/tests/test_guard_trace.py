"""Tests for the AAH guard observability helper (Gap C, post-#247 audit).

The trace helper at ``aah.core.guards._trace.trace`` is opt-in:
``AAH_GUARD_TRACE=1`` enables one stderr line per guard
invocation that ends without blocking. These tests pin:
  1. The helper itself: env-var gating, output format.
  2. Each instrumented guard emits a trace on its silent success/no-op
     paths when the env is set, and stays silent when it isn't.

Tests run each guard as a subprocess (matching how the harness
invokes hooks) so that env-var propagation and stderr capture
exercise the same code paths a production audit would.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from aah.core.guards._trace import trace


# ---------------------------------------------------------------------------
# Direct unit tests on the helper
# ---------------------------------------------------------------------------


class TestTraceHelper:
    def test_trace_off_by_default(self, capsys, monkeypatch):
        monkeypatch.delenv("AAH_GUARD_TRACE", raising=False)
        trace("some_guard", "allow", "some/file.py")
        captured = capsys.readouterr()
        assert captured.err == "", (
            f"trace must be silent when env unset; got {captured.err!r}"
        )

    def test_trace_enabled_emits_stderr(self, capsys, monkeypatch):
        monkeypatch.setenv("AAH_GUARD_TRACE", "1")
        trace("some_guard", "allow", "src/foo.py")
        captured = capsys.readouterr()
        assert "[aah-guard:trace]" in captured.err
        assert "some_guard" in captured.err
        assert "allow" in captured.err
        assert "target=src/foo.py" in captured.err

    def test_trace_other_value_off(self, capsys, monkeypatch):
        """Only the literal '1' enables — '0', 'true', 'yes' don't."""
        for val in ("0", "true", "yes", "True", "TRUE"):
            monkeypatch.setenv("AAH_GUARD_TRACE", val)
            trace("g", "allow", "x")
            captured = capsys.readouterr()
            assert captured.err == "", (
                f"env={val!r} must NOT enable tracing; got {captured.err!r}"
            )

    def test_trace_omits_target_when_empty(self, capsys, monkeypatch):
        monkeypatch.setenv("AAH_GUARD_TRACE", "1")
        trace("g", "noop")
        captured = capsys.readouterr()
        assert "[aah-guard:trace] g noop" in captured.err
        assert "target=" not in captured.err


# ---------------------------------------------------------------------------
# Per-guard subprocess tests
# ---------------------------------------------------------------------------


def _run_guard_subprocess(
    module: str,
    hook_input: dict,
    env_extra: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> tuple[int, str]:
    """Spawn a guard module as a subprocess. Returns (rc, stderr)."""
    proc_env = {**os.environ}
    proc_env.pop("AAH_CONTEXT", None)
    if env_extra:
        proc_env.update(env_extra)
    if cwd is None:
        with tempfile.TemporaryDirectory(prefix="trace-test-") as td:
            proc = subprocess.run(
                [sys.executable, "-m", module],
                input=json.dumps(hook_input),
                capture_output=True,
                text=True,
                cwd=td,
                env=proc_env,
            )
            return proc.returncode, proc.stderr
    proc = subprocess.run(
        [sys.executable, "-m", module],
        input=json.dumps(hook_input),
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=proc_env,
    )
    return proc.returncode, proc.stderr


class TestVerificationArtifactGuardTrace:
    def test_traces_allow_on_legitimate_write(self):
        """Both AAH_VERIFICATION_WRITE=1 (writer attestation) AND
        AAH_GUARD_TRACE=1 set → guard exits 0 + trace appears."""
        code, stderr = _run_guard_subprocess(
            "aah.core.guards.verification_artifact_guard",
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "/x/proj/.aah/build/test-results/F001.json",
                },
            },
            env_extra={
                "AAH_VERIFICATION_WRITE": "1",
                "AAH_GUARD_TRACE": "1",
            },
        )
        assert code == 0, f"expected allow; rc={code} stderr={stderr!r}"
        assert "[aah-guard:trace] verification_artifact_guard allow" in stderr

    def test_traces_noop_for_non_verification_path(self):
        code, stderr = _run_guard_subprocess(
            "aah.core.guards.verification_artifact_guard",
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "/x/proj/src/foo.py"},
            },
            env_extra={"AAH_GUARD_TRACE": "1"},
        )
        assert code == 0
        assert "[aah-guard:trace] verification_artifact_guard noop" in stderr


class TestDependencyPolicyGuardTrace:
    def test_traces_noop_when_no_policy_file(self):
        # Cwd has no .aah ancestor → policy lookup returns None.
        code, stderr = _run_guard_subprocess(
            "aah.core.guards.dependency_policy_guard",
            {
                "tool_name": "Bash",
                "tool_input": {"command": "npm install react"},
            },
            env_extra={"AAH_GUARD_TRACE": "1"},
        )
        assert code == 0
        assert "[aah-guard:trace] dependency_policy_guard noop" in stderr


class TestMockDataGuardTrace:
    def test_traces_noop_when_not_a_fixture_path(self):
        code, stderr = _run_guard_subprocess(
            "aah.core.guards.mock_data_consistency_guard",
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "/x/proj/src/foo.py"},
            },
            env_extra={"AAH_GUARD_TRACE": "1"},
        )
        assert code == 0
        assert "[aah-guard:trace] mock_data_consistency_guard noop" in stderr


class TestCircularImportGuardTrace:
    def test_traces_allow_when_no_cycle(self, tmp_path):
        # Create a non-cyclic Python file inside a project root.
        (tmp_path / ".aah").mkdir()
        src = tmp_path / "foo.py"
        src.write_text("# no imports\nx = 1\n")
        code, stderr = _run_guard_subprocess(
            "aah.core.guards.circular_import_guard",
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(src)},
            },
            env_extra={"AAH_GUARD_TRACE": "1"},
            cwd=tmp_path,
        )
        assert code == 0
        assert "[aah-guard:trace] circular_import_guard allow" in stderr

    def test_traces_noop_when_not_a_source_file(self):
        code, stderr = _run_guard_subprocess(
            "aah.core.guards.circular_import_guard",
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "/x/proj/README.md"},
            },
            env_extra={"AAH_GUARD_TRACE": "1"},
        )
        assert code == 0
        assert "[aah-guard:trace] circular_import_guard noop" in stderr
