"""Microsoft Agent Framework adapter — cloudless.MAFAgent base.

Inherits cloudless.Agent. Users implement `build()` to return an
`agent_framework.Agent` (the standard chat-client-backed agent) or any
object exposing async `run(prompt, *, stream=True, session=...)`.
cloudless drives the agent's streaming run and translates each
`AgentResponseUpdate` — whose `.contents` are unified
`agent_framework.Content` objects with a `.type` discriminator
(`text`, `text_reasoning`, `function_call`, `function_result`, `usage`)
— into cloudless Chunks per Q16.

Example:
    @cloudless.agent(name="planner", framework="maf")
    class PlannerAgent(cloudless.MAFAgent):
        def build(self):
            from agent_framework import Agent
            from agent_framework_bedrock import BedrockChatClient
            return Agent(
                chat_client=BedrockChatClient(model_id="us.amazon.nova-micro-v1:0"),
                instructions="Plan succinctly.",
                tools=[...],
            )
"""
from __future__ import annotations

from abc import abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from cloudless.agent import Agent
from cloudless.chunks import (
    Chunk,
    FinalChunk,
    ReasoningChunk,
    TextChunk,
    ToolCallChunk,
    ToolResultChunk,
)


class MAFAgent(Agent):
    """Base class for Microsoft Agent Framework-backed cloudless agents.

    Subclass and implement `build()` returning an `agent_framework.Agent`
    (or any object exposing async `run(prompt, *, stream=True, ...)` that
    yields `AgentResponseUpdate` objects).

    Session bridging: a per-cloudless-session MAF session is created on
    demand so multi-turn state can be reconstructed by the cloud runtime.
    """

    _maf_agent: Any = None
    _maf_sessions: dict[str, Any] = None  # type: ignore[assignment]

    @abstractmethod
    def build(self) -> Any:
        """Construct and return an `agent_framework.Agent` instance.

        Called once per cloudless Agent instance (lazily on first
        `query`).
        """
        raise NotImplementedError

    def _agent(self) -> Any:
        if self._maf_agent is None:
            self._maf_agent = self.build()
        if self._maf_sessions is None:
            self._maf_sessions = {}
        return self._maf_agent

    def _session(self, session_id: str) -> Any | None:
        """Return (and lazily create) the MAF session for a cloudless session."""
        agent = self._agent()
        if session_id not in self._maf_sessions:
            create_session = getattr(agent, "create_session", None)
            if callable(create_session):
                try:
                    self._maf_sessions[session_id] = create_session()
                except Exception:
                    self._maf_sessions[session_id] = None
            else:
                self._maf_sessions[session_id] = None
        return self._maf_sessions[session_id]

    async def query(self, ctx: Any, prompt: str) -> AsyncIterator[Chunk]:
        """Drive the MAF agent's streaming `run`; yield cloudless Chunks."""
        agent = self._agent()
        session_id = ctx.session.id if ctx else "cloudless-session"
        session = self._session(session_id)

        run = getattr(agent, "run", None)
        if run is None:
            raise TypeError(
                f"MAFAgent.build() returned {type(agent).__name__!r} which has "
                "no .run(...) method. Expected an agent_framework.Agent."
            )

        kwargs: dict[str, Any] = {"stream": True}
        if session is not None:
            kwargs["session"] = session

        # `run(prompt, stream=True, session=...)` returns a ResponseStream
        # which is an async-iterable of AgentResponseUpdate.
        stream = run(prompt, **kwargs)

        final_state: dict[str, Any] | None = None
        async for update in stream:
            for chunk in self._translate(update):
                yield chunk
            captured = self._capture_state(update)
            if captured is not None:
                final_state = captured

        yield FinalChunk(state=final_state)

    # ------------------------------------------------------------------ #
    # MAF AgentResponseUpdate → cloudless Chunk translation
    # ------------------------------------------------------------------ #

    @staticmethod
    def _translate(update: Any) -> list[Chunk]:
        """Map a single MAF AgentResponseUpdate to zero-or-more Chunks.

        MAF uses a unified `Content` class with a `.type` discriminator;
        every Content has every possible field, but only those relevant
        to the discriminated type are populated.
        """
        out: list[Chunk] = []
        contents = getattr(update, "contents", None)
        if not contents:
            text = getattr(update, "text", None)
            if isinstance(text, str) and text:
                out.append(TextChunk(text=text))
            return out

        for content in contents:
            ctype = getattr(content, "type", None) or ""

            if ctype == "text_reasoning":
                text = getattr(content, "text", "") or ""
                if text:
                    out.append(ReasoningChunk(text=text))
                continue

            if ctype == "text":
                text = getattr(content, "text", "") or ""
                if text:
                    out.append(TextChunk(text=text))
                continue

            if ctype == "function_call":
                args = getattr(content, "arguments", None) or {}
                if isinstance(args, str):
                    try:
                        import json

                        parsed = json.loads(args)
                        args = parsed if isinstance(parsed, dict) else {"input": parsed}
                    except Exception:
                        args = {"input": args}
                if not isinstance(args, dict):
                    args = {"input": args}
                name = getattr(content, "name", None) or "unknown"
                call_id = getattr(content, "call_id", None)
                # Skip partial deltas with no name AND empty args
                if name == "unknown" and not args:
                    continue
                out.append(ToolCallChunk(name=name, args=args, call_id=call_id))
                continue

            if ctype == "function_result":
                name = getattr(content, "name", None) or "tool"
                result = getattr(content, "result", None)
                call_id = getattr(content, "call_id", None)
                exc = getattr(content, "exception", None)
                out.append(
                    ToolResultChunk(
                        name=name,
                        result=result if exc is None else str(exc),
                        call_id=call_id,
                        is_error=exc is not None,
                    )
                )
                continue

            # error, usage, code_interpreter_*, etc. → no direct chunk type;
            # left to the final-state snapshot below.

        return out

    @staticmethod
    def _capture_state(update: Any) -> dict[str, Any] | None:
        """If this update carries terminal usage info, snapshot it for FinalChunk."""
        contents = getattr(update, "contents", None) or []
        for content in contents:
            if getattr(content, "type", None) == "usage":
                usage_details = getattr(content, "usage_details", None)
                if usage_details is None:
                    continue
                if hasattr(usage_details, "model_dump"):
                    try:
                        return {"usage": usage_details.model_dump()}
                    except Exception:
                        pass
                if hasattr(usage_details, "to_dict"):
                    try:
                        return {"usage": usage_details.to_dict()}
                    except Exception:
                        pass
                if isinstance(usage_details, dict):
                    return {"usage": usage_details}
                return {"usage": str(usage_details)}

        finish_reason = getattr(update, "finish_reason", None)
        if finish_reason:
            return {"finish_reason": str(finish_reason)}
        return None
