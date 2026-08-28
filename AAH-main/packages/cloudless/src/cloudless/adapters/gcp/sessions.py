"""Vertex AI Sessions API integration.

Vertex AI offers a SessionsService for multi-turn agent conversation
state, separate from Memory Bank (which is for long-term distilled facts).
A `Session` is a thread of `Event`s — each event has a role + content.

cloudless surfaces this as an alternate Memory backend that's session-
scoped rather than fact-scoped. Use this when:
  - Your agent needs to recall the exact turn-by-turn conversation history
  - You don't need long-term cross-session memory

For long-term facts that survive past one conversation, use Memory Bank
(`backend="memory_bank"`) instead.

API surface mirrors the existing AgentCoreBackend so swapping is trivial.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from cloudless.catalog.memory import MemoryEvent, MemoryRecord


class VertexSessionsBackend:
    """Memory backend that uses Vertex AI's SessionsService.

    Args:
        agent_engine_name: Full Agent Engine resource name (parent of sessions).
        project: GCP project.
        location: GCP region (default us-central1).
    """

    def __init__(
        self,
        *,
        agent_engine_name: str,
        project: str | None = None,
        location: str = "us-central1",
        client: Any = None,
    ) -> None:
        self.agent_engine_name = agent_engine_name
        self.project = project
        self.location = location
        if client is not None:
            self._client = client
        else:
            try:
                from google.cloud import aiplatform_v1beta1 as v1b
            except ImportError as e:
                raise RuntimeError(
                    "google-cloud-aiplatform[agent-engines] required for VertexSessionsBackend"
                ) from e
            endpoint = f"{location}-aiplatform.googleapis.com"
            self._client = v1b.SessionServiceClient(
                client_options={"api_endpoint": endpoint},
            )

    # ------------------------------------------------------------------ #
    # Scope semantics
    # ------------------------------------------------------------------ #

    def _session_name(self, scope: str) -> str:
        """Convert a cloudless scope to a Vertex session name.

        Sessions are scoped under the agent engine. We use the scope string
        as the session display ID (sanitized to alphanumeric+dash).
        """
        clean = "".join(c if c.isalnum() or c == "-" else "-" for c in scope)
        return f"{self.agent_engine_name}/sessions/{clean}"

    # ------------------------------------------------------------------ #
    # MemoryBackend Protocol
    # ------------------------------------------------------------------ #

    async def add_event(
        self, *, scope: str, role: str, content: str,
        metadata: dict | None = None,
    ) -> MemoryEvent:
        try:
            from google.cloud import aiplatform_v1beta1 as v1b
        except ImportError as e:
            raise RuntimeError("google-cloud-aiplatform required") from e
        # Ensure the session exists (idempotent — ignore AlreadyExists)
        try:
            self._client.create_session(
                request=v1b.CreateSessionRequest(
                    parent=self.agent_engine_name,
                    session=v1b.Session(name=self._session_name(scope)),
                ),
            )
        except Exception:
            pass  # session already exists

        # Append an event to the session
        request = v1b.AppendEventRequest(
            name=self._session_name(scope),
            event=v1b.SessionEvent(
                author=role,
                content=v1b.Content(
                    parts=[v1b.Part(text=content)],
                    role=role,
                ),
            ),
        )
        try:
            event = self._client.append_event(request=request)
        except Exception as e:
            raise self._translate(e) from e

        return MemoryEvent(
            id=event.name.rsplit("/", 1)[-1] if event.name else "unknown",
            scope=scope,
            role=role,
            content=content,
            timestamp=datetime.now(UTC),
            metadata=metadata or {},
        )

    async def list_events(self, *, scope: str, limit: int = 50) -> list[MemoryEvent]:
        try:
            from google.cloud import aiplatform_v1beta1 as v1b
        except ImportError as e:
            raise RuntimeError("google-cloud-aiplatform required") from e
        request = v1b.ListEventsRequest(
            parent=self._session_name(scope),
            page_size=limit,
        )
        try:
            page = self._client.list_events(request=request)
        except Exception as e:
            raise self._translate(e) from e

        events: list[MemoryEvent] = []
        for ev in page:
            content_text = ""
            if ev.content and ev.content.parts:
                content_text = ev.content.parts[0].text or ""
            events.append(MemoryEvent(
                id=ev.name.rsplit("/", 1)[-1],
                scope=scope,
                role=ev.author or "user",
                content=content_text,
                timestamp=datetime.now(UTC),
                metadata={},
            ))
            if len(events) >= limit:
                break
        return events

    async def recall_facts(
        self, *, scope: str, query: str, top_k: int = 5,
        similarity_threshold: float | None = None,
    ) -> list[MemoryRecord]:
        """Sessions don't expose semantic recall directly — list-and-filter.

        For real semantic recall, use Memory Bank backend instead.
        """
        events = await self.list_events(scope=scope, limit=200)
        # Naive keyword overlap as a recall proxy
        words = set(query.lower().split())
        scored: list[tuple[float, MemoryEvent]] = []
        for ev in events:
            overlap = len(words & set(ev.content.lower().split()))
            if overlap > 0:
                scored.append((overlap, ev))
        scored.sort(key=lambda kv: -kv[0])
        return [
            MemoryRecord(
                id=ev.id, text=ev.content, scope=scope, score=float(score),
                metadata={},
            )
            for score, ev in scored[:top_k]
        ]

    async def summarize_session(
        self, *, scope: str, session_id: str,
    ) -> MemoryRecord | None:
        events = await self.list_events(scope=scope, limit=20)
        if not events:
            return None
        summary = " | ".join(f"{e.role}: {e.content[:60]}" for e in events[-5:])
        return MemoryRecord(
            id=session_id, text=summary, scope=scope, score=None, metadata={},
        )

    async def get_preferences(self, *, scope: str) -> list[MemoryRecord]:
        """Sessions don't extract preferences — return an empty list.

        Use Memory Bank backend for preference extraction.
        """
        return []

    async def clear(self, *, scope: str) -> int:
        """Delete the session and all its events."""
        try:
            from google.cloud import aiplatform_v1beta1 as v1b
        except ImportError as e:
            raise RuntimeError("google-cloud-aiplatform required") from e
        try:
            self._client.delete_session(
                request=v1b.DeleteSessionRequest(name=self._session_name(scope)),
            )
            return 1
        except Exception:
            return 0

    @staticmethod
    def _translate(e: Exception) -> Exception:
        from cloudless.adapters.gcp.llm import GeminiBackend
        return GeminiBackend._translate(e)
