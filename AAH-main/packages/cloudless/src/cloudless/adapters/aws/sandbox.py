"""AWS AgentCore Code Interpreter backend for cloudless.Sandbox.

Uses the built-in `aws.codeinterpreter.v1` interpreter by default (no
need to CreateCodeInterpreter ourselves). Session lifecycle:
  - StartCodeInterpreterSession  (lazy on first execute)
  - InvokeCodeInterpreter(action=executeCode)  per call
  - StopCodeInterpreterSession   (on .close())

Per dossier 02: 2 vCPU / 8 GB / 10 GB disk per session; 8h max async; 100 MB payload.
"""
from __future__ import annotations

import threading

import boto3
from botocore.exceptions import ClientError

from cloudless.catalog.sandbox import SandboxResult


class CodeInterpreterBackend:
    """AgentCore Code Interpreter session manager."""

    def __init__(
        self, *,
        region: str = "us-east-1",
        code_interpreter_id: str = "aws.codeinterpreter.v1",
        session_idle_timeout_s: int = 900,  # 15 min default
    ) -> None:
        self.region = region
        self.code_interpreter_id = code_interpreter_id
        self.session_idle_timeout_s = session_idle_timeout_s
        # Two clients: data plane for invoke, control plane for start/stop
        self._data = boto3.client("bedrock-agentcore", region_name=region)
        self._session_id: str | None = None
        self._lock = threading.Lock()

    def _ensure_session(self) -> str:
        with self._lock:
            if self._session_id is not None:
                return self._session_id
            resp = self._data.start_code_interpreter_session(
                codeInterpreterIdentifier=self.code_interpreter_id,
                sessionTimeoutSeconds=self.session_idle_timeout_s,
            )
            self._session_id = resp["sessionId"]
            return self._session_id

    async def execute(
        self, *, code: str, language: str = "python",
        timeout_s: float = 60.0,
    ) -> SandboxResult:
        # AgentCore CI language values: "python", "javascript", "typescript"
        session_id = self._ensure_session()
        try:
            resp = self._data.invoke_code_interpreter(
                codeInterpreterIdentifier=self.code_interpreter_id,
                sessionId=session_id,
                name="executeCode",
                arguments={"language": language, "code": code},
            )
        except ClientError as e:
            raise self._translate(e) from e

        # Response is a streaming "stream" object; collect events.
        stream = resp.get("stream")
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_code = 0
        if stream is not None:
            for event in stream:
                # Each event is a dict with one of several keys
                result = event.get("result", {})
                if "structuredContent" in result:
                    sc = result["structuredContent"]
                    if "stdout" in sc:
                        stdout_parts.append(sc.get("stdout", ""))
                    if "stderr" in sc:
                        stderr_parts.append(sc.get("stderr", ""))
                    if "exitCode" in sc:
                        exit_code = int(sc["exitCode"])
                # Some events use "content" with text blocks
                for block in result.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        stdout_parts.append(block.get("text", ""))
        return SandboxResult(
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
            exit_code=exit_code,
        )

    async def close(self) -> None:
        with self._lock:
            sid = self._session_id
            self._session_id = None
        if sid is None:
            return
        try:
            self._data.stop_code_interpreter_session(
                codeInterpreterIdentifier=self.code_interpreter_id, sessionId=sid,
            )
        except ClientError:
            pass

    @staticmethod
    def _translate(e: ClientError) -> Exception:
        from cloudless.exceptions import (
            AuthenticationError,
            InvalidInputError,
            ThrottledError,
        )
        from cloudless.exceptions import (
            TimeoutError as CloudlessTimeoutError,
        )
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("ThrottlingException",):
            return ThrottledError(str(e))
        if code in ("AccessDeniedException", "UnauthorizedException"):
            return AuthenticationError(str(e))
        if code in ("ValidationException", "ResourceNotFoundException"):
            return InvalidInputError(str(e))
        if code in ("TimeoutException",):
            return CloudlessTimeoutError(str(e))
        return e
