from __future__ import annotations

import os
import shlex
import sys
import uuid
from pathlib import Path

from aah.core.common.execution import CommandSpec, run_bounded_command
from aah.core.common.redaction import sanitize_output
from aah.core.build.quality_checks import _run_quality_command


def _python(script: str, **overrides) -> CommandSpec:
    return CommandSpec(argv=[sys.executable, "-c", script], **overrides)

def test_pass_preserves_cwd_environment_and_text_output(tmp_path) -> None:
    outcome = run_bounded_command(
        _python(
            "import os; print(os.getcwd()); print(os.environ['AAH_EXECUTION_TEST'])",
            cwd=tmp_path,
            env={**os.environ, "AAH_EXECUTION_TEST": "visible"},
        )
    )
    assert outcome.state == "executed"
    assert outcome.ok is True
    assert outcome.returncode == 0
    assert outcome.stdout.splitlines() == [str(tmp_path), "visible"]
    assert outcome.stderr == ""

def test_nonzero_exit_is_an_executed_failure_with_both_streams() -> None:
    outcome = run_bounded_command(
        _python(
            "import sys; print('ordinary failure'); "
            "print('diagnostic', file=sys.stderr); raise SystemExit(7)"
        )
    )
    assert outcome.state == "executed"
    assert outcome.executed is True
    assert outcome.ok is False
    assert outcome.returncode == 7
    assert outcome.stdout == "ordinary failure\n"
    assert outcome.stderr == "diagnostic\n"

def test_missing_executable_has_a_distinct_outcome() -> None:
    missing = f"aah-command-that-does-not-exist-{uuid.uuid4().hex}"
    outcome = run_bounded_command(CommandSpec(argv=[missing]))
    assert outcome.state == "missing_executable"
    assert outcome.returncode == 127
    assert outcome.executed is False
    assert outcome.error

def test_timeout_stops_the_process_and_retains_partial_output() -> None:
    outcome = run_bounded_command(
        _python(
            "import time; print('started', flush=True); time.sleep(10)",
            timeout_sec=0.5,
        )
    )
    assert outcome.state == "timeout"
    assert outcome.returncode == 124
    assert outcome.stdout == "started\n"
    assert outcome.duration_ms < 3_000
    assert "timed out" in (outcome.error or "")

def test_oversized_output_is_bounded_without_exposing_partial_content() -> None:
    outcome = run_bounded_command(
        _python(
            "import os; os.write(1, b'a' * 200_000 + b'kept-tail')",
            text=False,
            max_output_bytes=1_024,
            max_output_chars=1_024,
        )
    )
    assert outcome.ok is True
    assert isinstance(outcome.stdout, bytes)
    assert outcome.stdout == b""
    assert outcome.stdout_total_bytes == 200_009
    assert outcome.stdout_truncated is True

def test_truncation_boundary_cannot_persist_a_secret_fragment() -> None:
    """A secret past the capture bound is dropped with the whole stream, never
    clipped mid-token.

    Checked at BOTH bounds: a hand-set 12 bytes, and the 256 KiB default every
    production caller actually gets. Clipping at the boundary would be the
    dangerous outcome — half a credential in an artifact is still a credential
    fragment, and a bound generous enough to look safe is where that would hide.
    """
    secret = "test-seeded-secret-value"
    code = f"import sys; sys.stdout.write('x'*300000 + 'password={secret}')"

    for spec in (_python(code, max_output_bytes=12), _python(code)):
        outcome = run_bounded_command(spec)
        assert outcome.stdout == ""
        assert secret not in outcome.stdout
        assert "secret-value" not in outcome.stdout
        assert outcome.stdout_truncated is True
        # The producer still ran and still wrote everything — truncation is a
        # capture decision, not a claim about the process.
        assert outcome.stdout_total_bytes == 300_000 + len(f"password={secret}")

def test_embedded_secret_from_real_process_is_redacted() -> None:
    secret = "test-seeded-secret-value"
    outcome = run_bounded_command(_python(f"print('password={secret}')"))
    sanitized = sanitize_output(outcome.stdout)
    assert outcome.ok is True
    assert secret in outcome.stdout
    assert secret not in sanitized
    assert sanitized == "password=***REDACTED***\n"
    quoted = sanitize_output('{"password": "super-secret-value"}')
    assert quoted == '{"password": ***REDACTED***}'
    url = sanitize_output("postgres://alice:p@ssword@host/db")
    assert url == "postgres://***REDACTED***@host/db"

def test_truncated_quality_output_is_no_signal() -> None:
    script = "import sys; print('critical'); print('x'*300000); raise SystemExit(1)"
    result = {"details": {}, "passed": True}
    outcome = _run_quality_command(
        result,
        shlex.join([sys.executable, "-c", script]),
        Path.cwd(),
        timeout=10,
        timeout_message="timed out",
        error_label="Linting",
    )
    assert outcome is None
    assert result["status"] == "no_signal"
    assert result["details"]["blocking_reason"] == "output_truncated"

def test_invalid_working_directory_is_an_execution_error(tmp_path) -> None:
    not_a_directory = tmp_path / "file.txt"
    not_a_directory.write_text("content", encoding="utf-8")
    outcome = run_bounded_command(_python("print('never runs')", cwd=not_a_directory))
    assert outcome.state == "execution_error"
    assert outcome.returncode == 126
    assert outcome.executed is False
    assert outcome.error
