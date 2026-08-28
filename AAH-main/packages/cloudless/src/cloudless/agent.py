"""Agent base class + `@cloudless.agent` decorator (Q10).

Users write:

    @cloudless.agent(name="support", interfaces=["http", "a2a"])
    class SupportAgent(cloudless.LangGraphAgent):
        def build(self) -> StateGraph:
            ...

The decorator records metadata on the class so `cloudless deploy` can
discover and emit the right deployment artifacts per Q4 (cloud-native
artifact per cloud) and Q6 (declarative interfaces).

Framework-specific bases (LangGraphAgent, StrandsAgent, ADKAgent,
ClaudeAgentSDKAgent, MAFAgent) live in cloudless.adapters.frameworks.*;
this module provides only the framework-agnostic abstract base.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from cloudless.chunks import Chunk

_T = TypeVar("_T", bound="Agent")


@dataclass(frozen=True)
class AgentMetadata:
    """Metadata recorded on an Agent class by `@cloudless.agent`.

    Read by:
      - `cloudless deploy` (CLI) to enumerate agents in src/agents/
      - The deploy planner (Q6) to emit one runtime per (protocol × auth_mode)
        tuple — see F11a; on AWS, AgentCore is single-protocol AND
        single-auth-mode per runtime.
    """

    name: str
    """Stable identifier, lowercase-hyphenated. Used in URL paths + manifest."""

    framework: str | None = None
    """One of {'langgraph', 'strands', 'adk', 'claude_sdk', 'maf', None}. None = custom."""

    interfaces: tuple[str, ...] = ("http",)
    """Protocols the agent serves. Subset of {'http', 'a2a', 'mcp', 'ag-ui'}."""

    description: str | None = None
    """Human-readable description; surfaces in the A2A agent card."""

    version: str = "0.1.0"
    """User-facing semver. Cloudless overlays an auto-version (Q18) at deploy."""

    tags: tuple[str, ...] = field(default_factory=tuple)
    """Optional grouping tags."""


class Agent(ABC):
    """Abstract base for every cloudless agent.

    Concrete framework adapters (LangGraphAgent, StrandsAgent, ADKAgent)
    inherit from this and provide framework-specific `build()` / `query()`
    implementations.

    Users typically don't subclass `Agent` directly — they pick a framework
    adapter base. The framework adapter is responsible for translating
    framework-native events into `Chunk` instances.
    """

    # Set by `@cloudless.agent`; accessed by the deploy planner.
    __cloudless_metadata__: AgentMetadata

    @abstractmethod
    async def query(self, ctx: Any, prompt: str) -> AsyncIterator[Chunk]:
        """Stream chunks in response to a prompt.

        Args:
            ctx: Per-invocation context (session_id, user, cost tracker,
                peer client). Concrete type defined by the runtime; left
                as `Any` here so the abstract base doesn't depend on the
                runtime module.
            prompt: The user's input.

        Yields:
            `Chunk` subclasses (TextChunk, ToolCallChunk, ...).
        """
        # The function body is unreachable for abstract methods, but Python
        # syntax requires SOMETHING in the function. `yield` makes mypy
        # recognize this as a generator function.
        if False:  # pragma: no cover
            yield
        raise NotImplementedError


def agent(
    name: str,
    *,
    framework: str | None = None,
    interfaces: tuple[str, ...] | list[str] = ("http",),
    description: str | None = None,
    version: str = "0.1.0",
    tags: tuple[str, ...] | list[str] = (),
) -> Callable[[type[_T]], type[_T]]:
    """Decorator that registers a class as a cloudless agent.

    Example:
        >>> import cloudless
        >>> @cloudless.agent(name="support", interfaces=["http", "a2a"])
        ... class SupportAgent(cloudless.LangGraphAgent):
        ...     def build(self): ...

    All keyword args become fields on `AgentMetadata` and are immutable
    once the class is decorated (frozen dataclass).
    """
    # Validate interfaces eagerly so typos fail at import, not at deploy.
    valid_interfaces = {"http", "a2a", "mcp", "ag-ui"}
    iface_tuple = tuple(interfaces)
    bad = set(iface_tuple) - valid_interfaces
    if bad:
        raise ValueError(
            f"@cloudless.agent: unknown interface(s) {sorted(bad)!r}. "
            f"Valid choices: {sorted(valid_interfaces)!r}"
        )
    if not iface_tuple:
        raise ValueError(
            "@cloudless.agent: `interfaces` must declare at least one protocol "
            "(commonly ['http'] for user-facing or ['a2a'] for peer-only)"
        )

    valid_frameworks = {"langgraph", "strands", "adk", "claude_sdk", "maf", None}
    if framework not in valid_frameworks:
        raise ValueError(
            f"@cloudless.agent: unknown framework {framework!r}. "
            f"Valid choices: {sorted(f for f in valid_frameworks if f)!r} or None"
        )

    metadata = AgentMetadata(
        name=name,
        framework=framework,
        interfaces=iface_tuple,
        description=description,
        version=version,
        tags=tuple(tags),
    )

    def _decorate(cls: type[_T]) -> type[_T]:
        if not issubclass(cls, Agent):
            raise TypeError(
                f"@cloudless.agent target {cls.__name__!r} must inherit from "
                f"cloudless.Agent (or a framework adapter base like "
                f"cloudless.LangGraphAgent)."
            )
        cls.__cloudless_metadata__ = metadata
        return cls

    return _decorate
