"""Strands adapter — cloudless.StrandsAgent base.

Inherits cloudless.Agent. Users implement `build()` to return a
`strands.Agent` instance; cloudless drives it via `stream_async` and
translates Strands' event stream into cloudless Chunks per Q16.
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


class StrandsAgent(Agent):
    """Base class for Strands-backed cloudless agents.

    Subclass and implement `build()` returning a `strands.Agent` instance.

    Example:
        @cloudless.agent(name="support", framework="strands")
        class SupportAgent(cloudless.StrandsAgent):
            def build(self):
                from strands import Agent
                return Agent(
                    name="support",
                    model="us.amazon.nova-micro-v1:0",
                    system_prompt="...",
                    tools=[...],
                )
    """

    _strands_agent: Any = None

    @abstractmethod
    def build(self) -> Any:
        """Construct and return a `strands.Agent` instance.

        Called once per cloudless Agent instance (lazily on first `query`).
        """
        raise NotImplementedError

    def _agent(self) -> Any:
        if self._strands_agent is None:
            self._strands_agent = self.build()
        return self._strands_agent

    async def query(self, ctx: Any, prompt: str) -> AsyncIterator[Chunk]:
        """Drive the Strands agent via `stream_async`; yield cloudless Chunks."""
        agent = self._agent()
        # Strands' stream_async yields events with shape:
        #   {"event": {"contentBlockDelta": {"delta": {"text": "..."}}}}
        #   {"event": {"contentBlockStart": {"start": {"toolUse": {...}}}}}
        #   {"event": {"messageStart": ...}, {"messageStop": ...}}
        # Plus higher-level events (tool calls, agent results) at the top.
        final_message: Any = None
        async for event in agent.stream_async(prompt):
            chunk = self._translate(event)
            if chunk is not None:
                yield chunk
            if isinstance(event, dict):
                if "result" in event:  # Strands top-level final result
                    final_message = event.get("result")
                elif "message" in event and not _is_streaming_event(event):
                    final_message = event.get("message")

        yield FinalChunk(state={"message": _coerce_serializable(final_message)})

    # ------------------------------------------------------------------ #
    # Strands event → cloudless Chunk translation
    # ------------------------------------------------------------------ #

    @staticmethod
    def _translate(event: Any) -> Chunk | None:
        if not isinstance(event, dict):
            return None

        # Text deltas
        if "data" in event and isinstance(event.get("data"), str):
            return TextChunk(text=event["data"])

        # Reasoning content (Claude/Nova extended thinking when enabled)
        if "reasoning_text" in event or "reasoning" in event:
            text = event.get("reasoning_text") or event.get("reasoning") or ""
            if text:
                return ReasoningChunk(text=str(text))

        # Tool calls
        if "current_tool_use" in event:
            tu = event["current_tool_use"] or {}
            if tu.get("name"):
                return ToolCallChunk(
                    name=tu["name"],
                    args=tu.get("input") or {},
                    call_id=tu.get("toolUseId"),
                )

        # Tool results — Strands surfaces these via the "message" stream when
        # the assistant turn includes toolResult blocks.
        if "tool_result" in event:
            tr = event["tool_result"]
            return ToolResultChunk(
                name=tr.get("name", "unknown"),
                result=tr.get("output") or tr.get("result"),
                call_id=tr.get("toolUseId"),
                is_error=bool(tr.get("isError")),
            )

        return None


def _is_streaming_event(event: dict[str, Any]) -> bool:
    """True if this is a per-chunk streaming event rather than a final one."""
    inner = event.get("event") or {}
    if isinstance(inner, dict):
        return any(k in inner for k in (
            "contentBlockDelta", "contentBlockStart", "contentBlockStop",
            "messageStart",
        ))
    return False


def _coerce_serializable(obj: Any) -> Any:
    """Best-effort coercion of Strands' result/message to JSON-friendly form."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool, list, dict)):
        return obj
    # Most Strands message objects have a .to_dict() or .__dict__
    for attr in ("to_dict", "model_dump", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    return str(obj)
