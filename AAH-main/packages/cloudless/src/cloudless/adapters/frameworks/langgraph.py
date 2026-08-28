"""LangGraph adapter — cloudless.LangGraphAgent base.

Inherits cloudless.Agent. Users implement `build()` to return a compiled
LangGraph StateGraph; cloudless drives it via `astream_events` and
translates LangGraph events into cloudless Chunks per Q16.
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


class LangGraphAgent(Agent):
    """Base class for LangGraph-backed cloudless agents.

    Subclass and implement `build()` returning a compiled `StateGraph`.
    The default `query()` runs the graph via `astream_events(version="v2")`
    and yields cloudless Chunks.

    Example:
        @cloudless.agent(name="support", framework="langgraph")
        class SupportAgent(cloudless.LangGraphAgent):
            def build(self):
                graph_builder = StateGraph(...)
                # ... add nodes / edges ...
                return graph_builder.compile()
    """

    # Cached compiled graph — built once per instance.
    _compiled_graph: Any = None

    @abstractmethod
    def build(self) -> Any:
        """Construct and compile the LangGraph StateGraph.

        Called once per Agent instance (lazily on first `query`). The
        returned object must have an `astream_events` async generator
        method (i.e., a compiled `StateGraph`).
        """
        raise NotImplementedError

    def _graph(self) -> Any:
        if self._compiled_graph is None:
            self._compiled_graph = self.build()
        return self._compiled_graph

    async def query(self, ctx: Any, prompt: str) -> AsyncIterator[Chunk]:
        """Run the LangGraph graph; stream cloudless chunks.

        Args:
            ctx: cloudless.Context for this invocation. Used to derive
                the LangGraph `thread_id` from `ctx.session.id`.
            prompt: User input. Wrapped as a single user message in
                `{"messages": [{"role": "user", "content": prompt}]}`.

        Yields:
            cloudless Chunks (TextChunk / ToolCallChunk / ToolResultChunk /
            FinalChunk).
        """
        graph = self._graph()

        # LangGraph configurable: thread_id maps to cloudless session.
        # See Q14 memory bridge — AgentCoreMemorySaver reads thread_id
        # to scope checkpoints in AgentCore Memory.
        config = {"configurable": {"thread_id": ctx.session.id}} if ctx else None

        final_state: Any = None
        async for event in graph.astream_events(
            {"messages": [{"role": "user", "content": prompt}]},
            config=config,
            version="v2",
        ):
            chunk = self._translate(event)
            if chunk is not None:
                yield chunk
            # Capture top-level graph end for the FinalChunk
            if (
                event.get("event") == "on_chain_end"
                and event.get("name") == "LangGraph"
            ):
                final_state = event.get("data", {}).get("output")

        yield FinalChunk(state=final_state if isinstance(final_state, dict) else None)

    # ------------------------------------------------------------------ #
    # LangGraph event → cloudless Chunk translation
    # ------------------------------------------------------------------ #

    @staticmethod
    def _translate(event: dict[str, Any]) -> Chunk | None:
        """Map a single LangGraph `astream_events` event to a Chunk (or None).

        Returns None for events that don't carry user-visible content
        (e.g., `on_chain_start`, intermediate state diffs).
        """
        kind = event.get("event")
        name = event.get("name", "")
        data = event.get("data", {})

        if kind == "on_chat_model_stream":
            # data.chunk is a langchain BaseMessage (AIMessageChunk usually).
            # Extract text content; ignore tool-call deltas (those come via
            # on_tool_start/end).
            chunk = data.get("chunk")
            content = _extract_text(chunk)
            if content:
                # Some Bedrock models emit reasoning content separately;
                # treat additional_kwargs["reasoning_content"] as Reasoning.
                reasoning = _extract_reasoning(chunk)
                if reasoning:
                    return ReasoningChunk(text=reasoning)
                return TextChunk(text=content)
            return None

        if kind == "on_tool_start":
            args = data.get("input", {}) or {}
            if not isinstance(args, dict):
                args = {"input": args}
            return ToolCallChunk(name=name, args=args)

        if kind == "on_tool_end":
            return ToolResultChunk(name=name, result=data.get("output"))

        return None


def _extract_text(chunk: Any) -> str:
    """Pull plain text out of a LangChain message chunk."""
    if chunk is None:
        return ""
    # AIMessageChunk has .content; can be str or list of dicts.
    content = getattr(chunk, "content", chunk)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


def _extract_reasoning(chunk: Any) -> str:
    """Pull reasoning content (Claude extended thinking) out of a message chunk."""
    if chunk is None:
        return ""
    kwargs = getattr(chunk, "additional_kwargs", None) or {}
    reasoning = kwargs.get("reasoning_content") or kwargs.get("thinking")
    if isinstance(reasoning, str):
        return reasoning
    if isinstance(reasoning, dict):
        return reasoning.get("text", "") or ""
    return ""
