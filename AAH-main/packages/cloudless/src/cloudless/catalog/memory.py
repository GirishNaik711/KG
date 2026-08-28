"""cloudless.Memory — unified memory primitive over AgentCore Memory / Memory Bank.

Per Q14, the user-facing API uses semantic VERBS not strategy names:

  memory = cloudless.Memory(backend="agentcore", memory_id=...)
  await memory.add_event(scope="user:42", role="user", content="my flight to Tokyo")
  facts = await memory.recall_facts(scope="user:42", query="travel destinations", top_k=5)

Internally, `recall_facts` maps to AWS SEMANTIC strategy / GCP topic filter;
`summarize_session` maps to SUMMARIZATION / topic; etc. The verb taxonomy
hides the cloud-specific strategy concept.

M1 ships:
  - In-memory backend (default for tests, `cloudless dev`)
  - AgentCore backend (AWS production)

GCP Memory Bank backend defers to M2.

Backend choice resolution:
  - Explicit:    cloudless.Memory(backend="agentcore", memory_id="...")
  - Auto in-mem: cloudless.Memory()  → InMemoryBackend
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

# --------------------------------------------------------------------- #
# Data shapes — all backend-neutral
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class MemoryRecord:
    """One distilled memory record returned from recall/summarize/etc."""

    id: str
    """Backend-assigned identifier."""

    text: str
    """The distilled fact / summary / preference text."""

    scope: str
    """Scope this record belongs to (user:42, session:abc, agent:global, ...)."""

    score: float | None = None
    """Similarity score for semantic-search results (0.0–1.0). None for non-search."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Extra context (timestamp, source event IDs, custom tags)."""


@dataclass(frozen=True)
class MemoryEvent:
    """A raw turn-by-turn event that gets distilled into MemoryRecords."""

    id: str
    scope: str
    role: str
    """One of 'user', 'assistant', 'tool', 'system'."""

    content: str
    timestamp: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------- #
# Backend Protocol — what concrete adapters implement
# --------------------------------------------------------------------- #


class MemoryBackend(Protocol):
    """Pluggable memory backend.

    Implementations:
      - cloudless.catalog.memory.InMemoryBackend     (dev / tests)
      - cloudless.adapters.aws.memory.AgentCoreBackend (M1 AWS prod)
      - cloudless.adapters.gcp.memory.MemoryBankBackend (M2 GCP prod)

    The backend Protocol uses async methods so concrete adapters can do
    network I/O. The in-memory implementation is also async for API
    consistency.
    """

    async def add_event(
        self, *, scope: str, role: str, content: str,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEvent: ...

    async def recall_facts(
        self, *, scope: str, query: str, top_k: int = 5,
    ) -> list[MemoryRecord]: ...

    async def summarize_session(
        self, *, scope: str, session_id: str,
    ) -> MemoryRecord | None: ...

    async def get_preferences(
        self, *, scope: str,
    ) -> list[MemoryRecord]: ...

    async def list_events(
        self, *, scope: str, limit: int = 50,
    ) -> list[MemoryEvent]: ...

    async def clear(self, *, scope: str) -> int:
        """Delete every record + event for the scope. Returns count removed."""
        ...


# --------------------------------------------------------------------- #
# In-memory backend — default for dev / tests
# --------------------------------------------------------------------- #


class InMemoryBackend:
    """Dev / test backend. No persistence, no semantic search — substring match.

    Pros: zero setup, deterministic, free.
    Cons: not realistic semantic recall; replace with AgentCoreBackend
          for any prompt-quality testing.
    """

    def __init__(self) -> None:
        self._events: list[MemoryEvent] = []
        self._record_counter = 0

    # ---- public verb API ----

    async def add_event(
        self, *, scope: str, role: str, content: str,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEvent:
        self._record_counter += 1
        ev = MemoryEvent(
            id=f"event-{self._record_counter}",
            scope=scope,
            role=role,
            content=content,
            timestamp=datetime.now(UTC),
            metadata=metadata or {},
        )
        self._events.append(ev)
        return ev

    async def recall_facts(
        self, *, scope: str, query: str, top_k: int = 5,
    ) -> list[MemoryRecord]:
        q = query.lower()
        hits = [
            MemoryRecord(
                id=f"rec-{i}", text=ev.content, scope=ev.scope,
                score=1.0 if q in ev.content.lower() else 0.5,
                metadata={"event_id": ev.id, "role": ev.role},
            )
            for i, ev in enumerate(self._events)
            if ev.scope == scope and ev.role in ("user", "assistant")
        ]
        hits.sort(key=lambda r: r.score or 0, reverse=True)
        return hits[:top_k]

    async def summarize_session(
        self, *, scope: str, session_id: str,
    ) -> MemoryRecord | None:
        relevant = [e for e in self._events
                    if e.scope == scope and e.metadata.get("session_id") == session_id]
        if not relevant:
            return None
        joined = " ".join(e.content for e in relevant[-10:])
        return MemoryRecord(
            id=f"summary-{session_id}",
            text=joined[:500],
            scope=scope,
            metadata={"session_id": session_id, "events": len(relevant)},
        )

    async def get_preferences(self, *, scope: str) -> list[MemoryRecord]:
        # In-memory backend has no extraction — return raw user-role events
        # tagged as preferences-like (best-effort).
        return [
            MemoryRecord(
                id=f"pref-{i}", text=ev.content, scope=ev.scope,
                metadata={"event_id": ev.id, "role": ev.role},
            )
            for i, ev in enumerate(self._events)
            if ev.scope == scope and ev.role == "user"
        ]

    async def list_events(self, *, scope: str, limit: int = 50) -> list[MemoryEvent]:
        return [e for e in self._events if e.scope == scope][-limit:]

    async def clear(self, *, scope: str) -> int:
        before = len(self._events)
        self._events = [e for e in self._events if e.scope != scope]
        return before - len(self._events)


# --------------------------------------------------------------------- #
# User-facing Memory class — thin facade over the chosen backend
# --------------------------------------------------------------------- #


class Memory:
    """Unified memory primitive (Q14).

    Example:
        memory = cloudless.Memory()                          # in-memory; default
        memory = cloudless.Memory(backend="agentcore",       # AWS production
                                  memory_id="mem-abc-123",
                                  region="us-east-1")

        await memory.add_event(scope=f"user:{ctx.user.id}",
                               role="user", content=prompt)
        facts = await memory.recall_facts(scope=f"user:{ctx.user.id}",
                                          query="travel preferences", top_k=5)
    """

    def __init__(
        self,
        *,
        backend: str | MemoryBackend = "in_memory",
        memory_id: str | None = None,
        region: str = "us-east-1",
        actor_id_template: str = "{scope}",
        agent_engine_name: str | None = None,
        location: str = "us-central1",
    ) -> None:
        """
        Args:
            backend: Either a string ("in_memory" | "agentcore") OR a pre-built
                MemoryBackend instance.
            memory_id: AgentCore Memory resource ID (required for backend="agentcore").
            region: AWS region (for AgentCore backend).
            actor_id_template: How to derive AgentCore's actorId from the cloudless
                scope. Default uses the scope string verbatim.
        """
        if isinstance(backend, str):
            self._backend = self._build_backend(
                backend, memory_id=memory_id, region=region,
                actor_id_template=actor_id_template,
                agent_engine_name=agent_engine_name, location=location,
            )
        else:
            self._backend = backend

    @staticmethod
    def _build_backend(
        name: str, *, memory_id: str | None, region: str,
        actor_id_template: str,
        agent_engine_name: str | None = None, location: str = "us-central1",
    ) -> MemoryBackend:
        if name == "in_memory":
            return InMemoryBackend()
        if name == "agentcore":
            if not memory_id:
                raise ValueError(
                    "backend='agentcore' requires memory_id. "
                    "Create one via boto3 bedrock-agentcore-control CreateMemory."
                )
            from cloudless.adapters.aws.memory import AgentCoreBackend
            return AgentCoreBackend(
                memory_id=memory_id, region=region,
                actor_id_template=actor_id_template,
            )
        if name == "memory_bank":
            if not agent_engine_name:
                raise ValueError(
                    "backend='memory_bank' requires agent_engine_name "
                    "(full reasoningEngines/.../<id> resource name)."
                )
            from cloudless.adapters.gcp.memory import MemoryBankBackend
            return MemoryBankBackend(
                agent_engine_name=agent_engine_name, location=location,
            )
        if name == "vertex_sessions":
            if not agent_engine_name:
                raise ValueError(
                    "backend='vertex_sessions' requires agent_engine_name."
                )
            from cloudless.adapters.gcp.sessions import VertexSessionsBackend
            return VertexSessionsBackend(
                agent_engine_name=agent_engine_name, location=location,
            )
        raise ValueError(f"Unknown memory backend: {name!r}")

    # ---- Verb-level API — delegates to backend ----

    async def add_event(
        self, *, scope: str, role: str, content: str,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEvent:
        return await self._backend.add_event(
            scope=scope, role=role, content=content, metadata=metadata,
        )

    async def recall_facts(
        self, *, scope: str, query: str, top_k: int = 5,
        similarity_threshold: float | None = None,
    ) -> list[MemoryRecord]:
        # Pass similarity_threshold through if the backend supports it.
        import inspect
        sig = inspect.signature(self._backend.recall_facts)
        if "similarity_threshold" in sig.parameters and similarity_threshold is not None:
            return await self._backend.recall_facts(  # type: ignore[call-arg]
                scope=scope, query=query, top_k=top_k,
                similarity_threshold=similarity_threshold,
            )
        return await self._backend.recall_facts(scope=scope, query=query, top_k=top_k)

    async def recall_facts_cross_actor(
        self, *, scopes: list[str], query: str, top_k: int = 5,
    ) -> list[MemoryRecord]:
        """Recall facts across multiple actor scopes (shared knowledge base).

        Useful when a "team" memory or "company-wide" memory spans several
        per-user scopes. Loops the recall call per scope and merges results
        ranked by score where available.
        """
        all_results: list[MemoryRecord] = []
        for scope in scopes:
            try:
                results = await self._backend.recall_facts(
                    scope=scope, query=query, top_k=top_k,
                )
                all_results.extend(results)
            except Exception:
                continue
        all_results.sort(
            key=lambda r: (r.score if r.score is not None else 0.0),
            reverse=True,
        )
        return all_results[:top_k]

    async def add_events_bulk(
        self,
        *,
        scope: str,
        events: list[dict],
    ) -> list[MemoryEvent]:
        """Add many events at once. Backends without a bulk API loop serially.

        Each event dict requires `role` and `content`; optional `metadata`.
        """
        results: list[MemoryEvent] = []
        for ev in events:
            rec = await self._backend.add_event(
                scope=scope,
                role=ev["role"],
                content=ev["content"],
                metadata=ev.get("metadata"),
            )
            results.append(rec)
        return results

    async def export_facts(
        self, *, scope: str, limit: int = 1000,
    ) -> list[dict]:
        """Export all events under `scope` as plain dicts for backup / migration.

        Returns list of {id, scope, role, content, timestamp, metadata}.
        """
        events = await self._backend.list_events(scope=scope, limit=limit)
        return [
            {
                "id": e.id,
                "scope": e.scope,
                "role": e.role,
                "content": e.content,
                "timestamp": (
                    e.timestamp.isoformat() if hasattr(e.timestamp, "isoformat")
                    else str(e.timestamp)
                ),
                "metadata": e.metadata,
            }
            for e in events
        ]

    async def import_facts(
        self, *, records: list[dict], default_scope: str | None = None,
    ) -> int:
        """Bulk-import previously exported events. Returns the count imported.

        Each record must include `role` and `content`; `scope` falls back to
        `default_scope` if not present on the record.
        """
        n = 0
        for r in records:
            scope = r.get("scope") or default_scope
            if not scope:
                raise ValueError("record without scope and no default_scope")
            await self._backend.add_event(
                scope=scope,
                role=r["role"],
                content=r["content"],
                metadata=r.get("metadata"),
            )
            n += 1
        return n

    async def summarize_session(
        self, *, scope: str, session_id: str,
    ) -> MemoryRecord | None:
        return await self._backend.summarize_session(
            scope=scope, session_id=session_id,
        )

    async def get_preferences(self, *, scope: str) -> list[MemoryRecord]:
        return await self._backend.get_preferences(scope=scope)

    async def list_events(self, *, scope: str, limit: int = 50) -> list[MemoryEvent]:
        return await self._backend.list_events(scope=scope, limit=limit)

    async def clear(self, *, scope: str) -> int:
        return await self._backend.clear(scope=scope)
