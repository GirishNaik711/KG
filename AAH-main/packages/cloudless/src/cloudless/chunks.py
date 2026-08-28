"""Typed Chunk taxonomy for streaming agent output (Q16).

Every `agent.query(...)` returns `AsyncIterator[Chunk]`. The framework
adapters in `cloudless.adapters.frameworks.*` translate the underlying
framework's events into this uniform stream. The cloud adapters in
`cloudless.adapters.aws.*` / `cloudless.adapters.gcp.*` translate the
stream onto the cloud's wire protocol (SSE on AgentCore, `stream_query()`
on Gemini Enterprise Agent Runtime).

Why typed chunks (rather than raw strings):
  - Frontend renderers can specialize per chunk type
  - Cost telemetry can attribute per chunk type (reasoning vs output)
  - A2A peers can react differently to tool calls vs final answers
  - Forward-compatible: new chunk types added without breaking the stream
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _ChunkBase(BaseModel):
    """Internal base — never used directly. Subclasses set `kind`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    """Discriminator field. Always matches the subclass name in snake_case."""


class TextChunk(_ChunkBase):
    """Token-by-token model output."""

    kind: str = Field(default="text", frozen=True)
    text: str


class ToolCallChunk(_ChunkBase):
    """Tool invocation started by the model."""

    kind: str = Field(default="tool_call", frozen=True)
    name: str
    args: dict[str, Any]
    call_id: str | None = None


class ToolResultChunk(_ChunkBase):
    """Result of a tool invocation."""

    kind: str = Field(default="tool_result", frozen=True)
    name: str
    result: Any
    call_id: str | None = None
    is_error: bool = False


class ReasoningChunk(_ChunkBase):
    """Extended-thinking content (Claude extended thinking, Gemini thoughts).

    Separated from `TextChunk` so frontends can fold/hide reasoning and
    cost telemetry can bill reasoning tokens distinctly (per F2 — Gemini
    2.5 thoughts_token_count is reported separately by the API).
    """

    kind: str = Field(default="reasoning", frozen=True)
    text: str


class StateChunk(_ChunkBase):
    """Graph-state snapshot (LangGraph / ADK state graphs)."""

    kind: str = Field(default="state", frozen=True)
    state: dict[str, Any]


class FinalChunk(_ChunkBase):
    """Terminal marker. Carries the final agent state if the framework supplies one."""

    kind: str = Field(default="final", frozen=True)
    state: dict[str, Any] | None = None


class PauseChunk(_ChunkBase):
    """HITL pause point (Q17 long-running tasks).

    The agent yields this to await human approval before proceeding.
    Runtime persists the task state under `resume_token`; a subsequent
    `cloudless.runtime.tasks.resume(resume_token, approval)` continues
    the agent from this point.
    """

    kind: str = Field(default="pause", frozen=True)
    resume_token: str
    """Opaque token identifying this pause point. Treat as a session ID."""

    reason: str = ""
    """Why the agent paused (e.g., "awaiting refund approval > $1000")."""

    pending_action: dict[str, Any] | None = None
    """Snapshot of what the agent intends to do once resumed."""

    expires_at: float | None = None
    """Unix epoch seconds for TTL (default: 24h post-pause)."""


class ErrorChunk(_ChunkBase):
    """Recoverable error mid-stream.

    Use for things the stream can survive (e.g., a fallback model was used,
    a non-critical write failed). Unrecoverable errors should raise an
    exception from the exception hierarchy instead.
    """

    kind: str = Field(default="error", frozen=True)
    error: str
    recoverable: bool = True
    details: dict[str, Any] | None = None


# Tagged-union type for type-narrowing in consumers
Chunk = (
    TextChunk
    | ToolCallChunk
    | ToolResultChunk
    | ReasoningChunk
    | StateChunk
    | PauseChunk
    | FinalChunk
    | ErrorChunk
)
"""Tagged union of every concrete chunk class. The `kind` field is the
discriminator. Pattern-match consumers should branch on `isinstance(c, X)`
or `c.kind == "..."`.
"""
