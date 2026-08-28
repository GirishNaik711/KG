"""cloudless.Sandbox — sandboxed code execution (Q9).

Backends:
  - LocalSubprocessBackend  — `cloudless dev` default. Forks a Python subprocess.
    Security warning: no isolation; do NOT run untrusted code locally.
  - CodeInterpreterBackend  — AWS AgentCore Code Interpreter (Firecracker microVM).
    Per dossier 02: 2 vCPU / 8 GB / 10 GB disk per session; 8h max async.

GCP backend (Agent Sandbox) defers to M2 because GCP exposes the sandbox
through a different API shape (Computer Use model vs. direct exec).
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SandboxResult:
    """Result of a single Sandbox.execute() call."""

    stdout: str
    stderr: str
    exit_code: int
    """0 on success; non-zero indicates failure (interpreter error or non-zero exit)."""


class SandboxBackend(Protocol):
    async def execute(
        self, *, code: str, language: str = "python",
        timeout_s: float = 60.0,
    ) -> SandboxResult: ...

    async def close(self) -> None: ...


# --------------------------------------------------------------------- #
# LocalSubprocessBackend — `cloudless dev` default
# --------------------------------------------------------------------- #


class LocalSubprocessBackend:
    """Run code via a local subprocess. SECURITY: no isolation.

    Per-instance workspace: a temp directory created lazily on first
    upload_file or execute. The subprocess runs with this dir as CWD so
    `open("data.csv")` resolves to the workspace.
    """

    def __init__(self) -> None:
        self._workspace: Path | None = None

    def _ws(self) -> Path:
        if self._workspace is None:
            self._workspace = Path(tempfile.mkdtemp(prefix="cloudless-sandbox-"))
        return self._workspace

    @property
    def workspace(self) -> Path:
        return self._ws()

    async def execute(
        self, *, code: str, language: str = "python",
        timeout_s: float = 60.0,
    ) -> SandboxResult:
        if language != "python":
            return SandboxResult(
                stdout="",
                stderr=f"LocalSubprocessBackend only supports python (got {language!r})",
                exit_code=1,
            )
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(code)
            script = f.name
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, script,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=str(self._ws()),
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            except TimeoutError:
                proc.kill()
                return SandboxResult(stdout="", stderr="execution timed out", exit_code=124)
            return SandboxResult(
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                exit_code=proc.returncode or 0,
            )
        finally:
            try:
                Path(script).unlink()
            except OSError:
                pass

    async def upload_file(self, *, name: str, content: bytes) -> str:
        path = self._ws() / name
        path.write_bytes(content)
        return str(path)

    async def download_file(self, *, name: str) -> bytes:
        path = self._ws() / name
        if not path.is_file():
            raise FileNotFoundError(f"no such file in sandbox: {name!r}")
        return path.read_bytes()

    async def close(self) -> None:
        if self._workspace and self._workspace.exists():
            try:
                shutil.rmtree(self._workspace)
            except OSError:
                pass
        self._workspace = None


# --------------------------------------------------------------------- #
# User-facing Sandbox class
# --------------------------------------------------------------------- #


class Sandbox:
    """Unified sandbox primitive (Q9).

    Example:
        sandbox = cloudless.Sandbox()                          # local subprocess
        sandbox = cloudless.Sandbox(backend="agentcore")       # AWS AgentCore CodeInterpreter
        result = await sandbox.execute(code="print(2 + 2)", language="python")
        print(result.stdout)  # "4\\n"
    """

    def __init__(
        self,
        *,
        backend: str | SandboxBackend = "local",
        region: str = "us-east-1",
        code_interpreter_id: str = "aws.codeinterpreter.v1",
    ) -> None:
        if isinstance(backend, str):
            self._backend = self._build_backend(
                backend, region=region, code_interpreter_id=code_interpreter_id,
            )
        else:
            self._backend = backend

    @staticmethod
    def _build_backend(
        name: str, *, region: str, code_interpreter_id: str,
    ) -> SandboxBackend:
        if name == "local":
            return LocalSubprocessBackend()
        if name == "agentcore":
            from cloudless.adapters.aws.sandbox import CodeInterpreterBackend
            return CodeInterpreterBackend(
                region=region, code_interpreter_id=code_interpreter_id,
            )
        raise ValueError(f"Unknown sandbox backend: {name!r}")

    async def execute(
        self, *, code: str, language: str = "python",
        timeout_s: float = 60.0,
    ) -> SandboxResult:
        return await self._backend.execute(
            code=code, language=language, timeout_s=timeout_s,
        )

    async def close(self) -> None:
        await self._backend.close()

    # ------------------------------------------------------------------ #
    # File transfer (M2+ on AgentCore; LocalSubprocessBackend = tempdir)
    # ------------------------------------------------------------------ #

    async def upload_file(self, *, name: str, content: bytes) -> str:
        """Upload `content` into the sandbox under `name`. Returns the
        absolute path inside the sandbox (relative paths from `execute` resolve
        from the workspace root).

        Backend support:
          - LocalSubprocessBackend: writes to a per-sandbox tempdir, available
            via `Sandbox.workspace`.
          - CodeInterpreterBackend (AWS): writes via the Code Interpreter
            file API (`/sandbox/files/<name>`).
        """
        if not hasattr(self._backend, "upload_file"):
            raise NotImplementedError(
                f"backend {type(self._backend).__name__} does not support upload_file"
            )
        return await self._backend.upload_file(name=name, content=content)

    async def download_file(self, *, name: str) -> bytes:
        """Download a file produced by sandbox execution."""
        if not hasattr(self._backend, "download_file"):
            raise NotImplementedError(
                f"backend {type(self._backend).__name__} does not support download_file"
            )
        return await self._backend.download_file(name=name)

    async def execute_long_running(
        self, *, code: str, language: str = "python", max_seconds: float = 3600.0,
    ) -> SandboxResult:
        """Execute with a long timeout (default 1 hour, up to AgentCore's 8h max).

        Use for batch data processing, long-running ML jobs. Local backend
        respects the same timeout. AgentCore CodeInterpreter supports up to
        8 hours per session.
        """
        return await self._backend.execute(
            code=code, language=language, timeout_s=max_seconds,
        )
