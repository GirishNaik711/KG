"""Anthropic Claude Agent SDK adapter — cloudless.ClaudeAgentSDKAgent base.

Inherits cloudless.Agent. Users implement `build()` to return a
`claude_agent_sdk.ClaudeAgentOptions` (or None for defaults); cloudless
drives `claude_agent_sdk.query()` and translates the resulting
AssistantMessage / UserMessage / ResultMessage stream — with TextBlock,
ThinkingBlock, ToolUseBlock, and ToolResultBlock content — into
cloudless Chunks per Q16.

Example:
    @cloudless.agent(name="research", framework="claude_sdk")
    class ResearchAgent(cloudless.ClaudeAgentSDKAgent):
        def build(self):
            from claude_agent_sdk import ClaudeAgentOptions
            return ClaudeAgentOptions(
                system_prompt="Be concise.",
                allowed_tools=["Read", "Grep"],
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


class ClaudeAgentSDKAgent(Agent):
    """Base class for Anthropic Claude Agent SDK-backed cloudless agents.

    Subclass and implement `build()` returning a
    `claude_agent_sdk.ClaudeAgentOptions` (or None to use SDK defaults).
    """

    _claude_options: Any = None
    _claude_options_built: bool = False

    @abstractmethod
    def build(self) -> Any:
        """Construct and return a `ClaudeAgentOptions` (or None).

        Called once per cloudless Agent instance (lazily on first
        `query`).
        """
        raise NotImplementedError

    def _options(self) -> Any:
        if not self._claude_options_built:
            self._claude_options = self.build()
            self._claude_options_built = True
        return self._claude_options

    async def query(self, ctx: Any, prompt: str) -> AsyncIterator[Chunk]:
        """Drive the Claude Agent SDK via `query()`; yield cloudless Chunks."""
        from claude_agent_sdk import query as claude_query  # type: ignore

        options = self._options()
        kwargs: dict[str, Any] = {"prompt": prompt}
        if options is not None:
            kwargs["options"] = options

        final_state: dict[str, Any] | None = None
        async for message in claude_query(**kwargs):
            for chunk in self._translate(message):
                yield chunk
            if self._is_result(message):
                final_state = self._capture_state(message)

        yield FinalChunk(state=final_state)

    # ------------------------------------------------------------------ #
    # Claude SDK message → cloudless Chunk translation
    # ------------------------------------------------------------------ #

    @staticmethod
    def _translate(message: Any) -> list[Chunk]:
        """Map a single Claude SDK message to zero-or-more Chunks.

        Duck-typed: we recognise messages by attribute shape so the
        adapter remains forward-compatible with non-breaking SDK changes.
        """
        out: list[Chunk] = []
        content = getattr(message, "content", None)
        if not content:
            return out
        if not isinstance(content, list):
            return out
        for block in content:
            cls_name = type(block).__name__
            text = getattr(block, "text", None)
            thinking = getattr(block, "thinking", None)
            tool_name = getattr(block, "name", None)
            tool_input = getattr(block, "input", None)
            tool_id = getattr(block, "id", None)
            tool_use_id = getattr(block, "tool_use_id", None)
            tool_content = getattr(block, "content", None)
            is_error = getattr(block, "is_error", False)

            if cls_name == "ThinkingBlock" or thinking:
                txt = thinking if isinstance(thinking, str) else text or ""
                if txt:
                    out.append(ReasoningChunk(text=txt))
                continue

            if cls_name == "TextBlock" or (text and tool_name is None and tool_use_id is None):
                if text:
                    out.append(TextChunk(text=text))
                continue

            if cls_name == "ToolUseBlock" or (tool_name and tool_input is not None):
                args = tool_input if isinstance(tool_input, dict) else {"input": tool_input}
                out.append(
                    ToolCallChunk(name=tool_name or "unknown", args=args, call_id=tool_id)
                )
                continue

            if cls_name == "ToolResultBlock" or tool_use_id is not None:
                out.append(
                    ToolResultChunk(
                        name="tool",
                        result=tool_content,
                        call_id=tool_use_id,
                        is_error=bool(is_error),
                    )
                )
                continue

        return out

    @staticmethod
    def _is_result(message: Any) -> bool:
        return type(message).__name__ == "ResultMessage" or hasattr(message, "total_cost_usd")

    @staticmethod
    def _capture_state(message: Any) -> dict[str, Any] | None:
        """Extract a JSON-friendly snapshot from a ResultMessage."""
        state: dict[str, Any] = {}
        for attr in ("result", "session_id", "num_turns", "total_cost_usd", "duration_ms"):
            val = getattr(message, attr, None)
            if val is not None:
                state[attr] = val
        usage = getattr(message, "usage", None)
        if isinstance(usage, dict):
            state["usage"] = usage
        return state or None


# Backwards-compatible alias for the shorter import path.
ClaudeSDKAgent = ClaudeAgentSDKAgent
