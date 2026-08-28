"""Google ADK adapter — cloudless.ADKAgent base.

Inherits cloudless.Agent. Users implement `build()` to return a
`google.adk.agents.Agent` (or `LlmAgent`) instance; cloudless drives it
via `google.adk.runners.InMemoryRunner.run_async` and translates ADK
Events (Content / Part with text / function_call / function_response)
into cloudless Chunks per Q16.

Example:
    @cloudless.agent(name="support", framework="adk")
    class SupportAgent(cloudless.ADKAgent):
        def build(self):
            from google.adk.agents import Agent
            return Agent(
                name="support",
                model="gemini-2.0-flash",
                instruction="Answer succinctly.",
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


class ADKAgent(Agent):
    """Base class for Google ADK-backed cloudless agents.

    Subclass and implement `build()` returning a `google.adk.agents.Agent`
    (or compatible `LlmAgent` / `SequentialAgent` etc.). The default
    `query()` wires the agent into an `InMemoryRunner`, drives it via
    `run_async`, and yields cloudless Chunks.

    Session bridging: the ADK session is created with `user_id` and
    `session_id` derived from `ctx.session.id` so cloudless's session
    identity is preserved through the ADK runner.
    """

    _adk_agent: Any = None
    _adk_runner: Any = None
    _adk_app_name: str = "cloudless"

    @abstractmethod
    def build(self) -> Any:
        """Construct and return a `google.adk.agents.Agent` instance.

        Called once per cloudless Agent instance (lazily on first
        `query`).
        """
        raise NotImplementedError

    def _agent(self) -> Any:
        if self._adk_agent is None:
            self._adk_agent = self.build()
        return self._adk_agent

    def _runner(self) -> Any:
        if self._adk_runner is None:
            from google.adk.runners import InMemoryRunner

            metadata = getattr(self, "__cloudless_metadata__", None)
            if metadata is not None and getattr(metadata, "name", None):
                self._adk_app_name = metadata.name
            self._adk_runner = InMemoryRunner(
                agent=self._agent(), app_name=self._adk_app_name
            )
        return self._adk_runner

    async def query(self, ctx: Any, prompt: str) -> AsyncIterator[Chunk]:
        """Drive the ADK agent via `runner.run_async`; yield cloudless Chunks."""
        from google.genai.types import Content, Part

        runner = self._runner()
        session_service = runner.session_service

        session_id = ctx.session.id if ctx else "cloudless-session"
        user_id = "cloudless-user"
        if ctx and getattr(ctx, "user", None) is not None:
            try:
                user_id = ctx.user.id  # type: ignore[union-attr]
            except Exception:
                user_id = "cloudless-user"

        # Idempotent session create — get_session first, create if absent.
        try:
            existing = await session_service.get_session(
                app_name=self._adk_app_name,
                user_id=user_id,
                session_id=session_id,
            )
        except Exception:
            existing = None
        if existing is None:
            await session_service.create_session(
                app_name=self._adk_app_name,
                user_id=user_id,
                session_id=session_id,
            )

        message = Content(role="user", parts=[Part(text=prompt)])

        final_state: dict[str, Any] | None = None
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=message,
        ):
            for chunk in self._translate(event):
                yield chunk
            if self._is_final(event):
                final_state = self._capture_state(event)

        yield FinalChunk(state=final_state)

    # ------------------------------------------------------------------ #
    # ADK Event → cloudless Chunk translation
    # ------------------------------------------------------------------ #

    @staticmethod
    def _translate(event: Any) -> list[Chunk]:
        """Map a single ADK Event to zero-or-more Chunks."""
        out: list[Chunk] = []
        content = getattr(event, "content", None)
        if content is None:
            return out
        parts = getattr(content, "parts", None) or []
        for part in parts:
            text = getattr(part, "text", None)
            # ADK marks chain-of-thought parts with `thought=True`
            if text:
                if getattr(part, "thought", False):
                    out.append(ReasoningChunk(text=text))
                else:
                    out.append(TextChunk(text=text))
                continue

            fc = getattr(part, "function_call", None)
            if fc is not None:
                args = getattr(fc, "args", None) or {}
                if not isinstance(args, dict):
                    args = {"input": args}
                out.append(
                    ToolCallChunk(
                        name=getattr(fc, "name", "unknown"),
                        args=args,
                        call_id=getattr(fc, "id", None),
                    )
                )
                continue

            fr = getattr(part, "function_response", None)
            if fr is not None:
                response = getattr(fr, "response", None)
                # ADK wraps results in {"result": ...} sometimes
                if isinstance(response, dict) and set(response.keys()) == {"result"}:
                    response = response["result"]
                out.append(
                    ToolResultChunk(
                        name=getattr(fr, "name", "unknown"),
                        result=response,
                        call_id=getattr(fr, "id", None),
                    )
                )
                continue

        return out

    @staticmethod
    def _is_final(event: Any) -> bool:
        fn = getattr(event, "is_final_response", None)
        if callable(fn):
            try:
                return bool(fn())
            except Exception:
                return False
        return False

    @staticmethod
    def _capture_state(event: Any) -> dict[str, Any] | None:
        """Extract a JSON-friendly state snapshot from a final ADK event."""
        content = getattr(event, "content", None)
        if content is None:
            return None
        parts = getattr(content, "parts", None) or []
        texts = [p.text for p in parts if getattr(p, "text", None)]
        if not texts:
            return None
        return {"final_text": "".join(texts)}
