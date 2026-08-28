"""Bounded subprocess execution with explicit, inspectable outcomes."""

from __future__ import annotations

import locale
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Sequence, TypeAlias


CommandState: TypeAlias = Literal[
    "executed", "missing_executable", "timeout", "execution_error"
]
CommandOutput: TypeAlias = str | bytes

_DEFAULT_OUTPUT_LIMIT = 256 * 1024
_READ_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """Bounded subprocess specification; ``env=None`` inherits the environment."""

    argv: str | Sequence[str]
    cwd: str | os.PathLike[str] | None = None
    env: Mapping[str, str] | None = None
    shell: bool = False
    timeout_sec: float = 60.0
    text: bool = True
    encoding: str | None = None
    errors: str = "replace"
    max_output_bytes: int = _DEFAULT_OUTPUT_LIMIT
    max_output_chars: int = _DEFAULT_OUTPUT_LIMIT

    def __post_init__(self) -> None:
        if not isinstance(self.argv, str):
            object.__setattr__(self, "argv", tuple(self.argv))
        if not self.argv:
            raise ValueError("argv must not be empty")
        if self.timeout_sec <= 0:
            raise ValueError("timeout_sec must be greater than zero")
        if self.max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be greater than zero")
        if self.max_output_chars <= 0:
            raise ValueError("max_output_chars must be greater than zero")


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """Result of a command, including failure category and capture metadata."""

    state: CommandState
    returncode: int
    stdout: CommandOutput
    stderr: CommandOutput
    duration_ms: int
    error: str | None = None
    stdout_total_bytes: int = 0
    stderr_total_bytes: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    @property
    def executed(self) -> bool:
        return self.state == "executed"

    @property
    def ok(self) -> bool:
        return self.executed and self.returncode == 0


class _BoundedBytes:
    __slots__ = ("_buffer", "limit", "total")

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.total = 0
        self._buffer = bytearray()

    def append(self, chunk: bytes) -> None:
        self.total += len(chunk)
        remaining = self.limit - len(self._buffer)
        if remaining > 0:
            self._buffer.extend(chunk[:remaining])

    def value(self) -> bytes:
        return bytes(self._buffer)


def _drain(stream, sink: _BoundedBytes) -> None:
    try:
        while chunk := stream.read(_READ_CHUNK_SIZE):
            sink.append(chunk)
    except (OSError, ValueError):
        # Closing a timed-out process can invalidate its pipe while a reader is
        # draining. The bytes already retained remain valid diagnostics.
        return


def _stop_process(proc: subprocess.Popen[bytes]) -> None:
    """Stop the child and, on POSIX, its isolated process group."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait(timeout=1)
    except (subprocess.SubprocessError, OSError):
        pass


def _render_output(raw: bytes, spec: CommandSpec) -> tuple[CommandOutput, bool]:
    if not spec.text:
        return raw, False
    encoding = spec.encoding or locale.getpreferredencoding(False)
    rendered = raw.decode(encoding, errors=spec.errors)
    if len(rendered) <= spec.max_output_chars:
        return rendered, False
    return rendered[-spec.max_output_chars :], True


def _empty_output(spec: CommandSpec) -> CommandOutput:
    return "" if spec.text else b""


def _outcome_without_process(
    spec: CommandSpec,
    *,
    state: Literal["missing_executable", "execution_error"],
    returncode: int,
    started: float,
    error: Exception,
) -> CommandOutcome:
    empty = _empty_output(spec)
    return CommandOutcome(
        state=state,
        returncode=returncode,
        stdout=empty,
        stderr=empty,
        duration_ms=int((time.monotonic() - started) * 1000),
        error=str(error),
    )


def run_bounded_command(spec: CommandSpec) -> CommandOutcome:
    """Run with bounded output; synthetic timeout/error codes are 124/127/126."""
    started = time.monotonic()
    popen_kwargs = {
        "cwd": spec.cwd,
        "env": spec.env,
        "shell": spec.shell,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": False,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(spec.argv, **popen_kwargs)
    except FileNotFoundError as exc:
        cwd_missing = spec.cwd is not None and not Path(spec.cwd).is_dir()
        state = "execution_error" if cwd_missing else "missing_executable"
        return _outcome_without_process(
            spec,
            state=state,
            returncode=126 if cwd_missing else 127,
            started=started,
            error=exc,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return _outcome_without_process(
            spec,
            state="execution_error",
            returncode=126,
            started=started,
            error=exc,
        )

    stdout_sink = _BoundedBytes(spec.max_output_bytes)
    stderr_sink = _BoundedBytes(spec.max_output_bytes)
    readers = (
        threading.Thread(target=_drain, args=(proc.stdout, stdout_sink), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, stderr_sink), daemon=True),
    )
    for reader in readers:
        reader.start()

    state: CommandState = "executed"
    returncode = 126
    error: str | None = None
    try:
        returncode = proc.wait(timeout=spec.timeout_sec)
    except subprocess.TimeoutExpired:
        state = "timeout"
        returncode = 124
        error = f"command timed out after {spec.timeout_sec:g}s"
        _stop_process(proc)
    except (OSError, subprocess.SubprocessError) as exc:
        state = "execution_error"
        error = str(exc)
        _stop_process(proc)

    for reader in readers:
        reader.join(timeout=1)

    raw_stdout = stdout_sink.value()
    raw_stderr = stderr_sink.value()
    stdout, stdout_chars_truncated = _render_output(raw_stdout, spec)
    stderr, stderr_chars_truncated = _render_output(raw_stderr, spec)
    stdout_truncated = stdout_sink.total > len(raw_stdout) or stdout_chars_truncated
    stderr_truncated = stderr_sink.total > len(raw_stderr) or stderr_chars_truncated
    # Never expose an arbitrary fragment from an incomplete stream. Redaction
    # happens in consumers and cannot safely identify a credential whose key
    # or value was split at the capture boundary.
    if stdout_truncated:
        stdout = _empty_output(spec)
    if stderr_truncated:
        stderr = _empty_output(spec)
    return CommandOutcome(
        state=state,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_ms=int((time.monotonic() - started) * 1000),
        error=error,
        stdout_total_bytes=stdout_sink.total,
        stderr_total_bytes=stderr_sink.total,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )
