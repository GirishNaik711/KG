"""AgentCore Memory backend for cloudless.Memory.

Maps the cloudless verb taxonomy (Q14) to AgentCore's API:

  Verb              | AgentCore call
  ------------------|------------------------------------------------------
  add_event         | bedrock-agentcore:CreateEvent
  recall_facts      | bedrock-agentcore:RetrieveMemoryRecords (SEMANTIC strategy)
  summarize_session | bedrock-agentcore:RetrieveMemoryRecords (SUMMARIZATION strategy)
  get_preferences   | bedrock-agentcore:RetrieveMemoryRecords (USER_PREFERENCE strategy)
  list_events       | bedrock-agentcore:ListEvents
  clear             | bedrock-agentcore:DeleteEvent loop

The memory resource itself is NOT created by this backend — users must
provision an AgentCore Memory resource (via boto3 or the cloudless deploy
adapter in M2). This backend just reads/writes against an existing memory_id.

Scope semantics:
  - cloudless scope strings (e.g. "user:42") are passed through directly
    as the AgentCore actorId.
  - AgentCore sessionId is taken from a metadata field if present;
    otherwise we use the actorId as the session too (single-session-per-scope).

Per dossier 02, AgentCore Memory long-term records are async-extracted —
a recall_facts() right after add_event() may not see the new record
immediately. Production code should account for this.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError

from cloudless.catalog.memory import MemoryEvent, MemoryRecord


class AgentCoreBackend:
    """AgentCore Memory data-plane client wrapped to the cloudless backend Protocol."""

    def __init__(
        self,
        *,
        memory_id: str,
        region: str = "us-east-1",
        actor_id_template: str = "{scope}",
        default_session_id: str | None = None,
    ) -> None:
        self.memory_id = memory_id
        self.region = region
        self.actor_id_template = actor_id_template
        self._default_session_id = default_session_id or f"cloudless-default-{uuid.uuid4().hex[:8]}"
        self._client = boto3.client("bedrock-agentcore", region_name=region)

    # ------------------------------------------------------------------ #
    # Scope → AgentCore (actorId, sessionId) mapping
    # ------------------------------------------------------------------ #

    def _actor_id(self, scope: str) -> str:
        return self.actor_id_template.format(scope=scope)

    def _session_id(self, metadata: dict[str, Any] | None) -> str:
        if metadata and metadata.get("session_id"):
            return str(metadata["session_id"])
        return self._default_session_id

    # ------------------------------------------------------------------ #
    # Verbs
    # ------------------------------------------------------------------ #

    async def add_event(
        self, *, scope: str, role: str, content: str,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEvent:
        actor_id = self._actor_id(scope)
        session_id = self._session_id(metadata)

        # AgentCore role values: ASSISTANT, USER, TOOL, OTHER
        agentcore_role = {
            "user": "USER", "assistant": "ASSISTANT", "tool": "TOOL", "system": "OTHER",
        }.get(role.lower(), "OTHER")

        try:
            resp = self._client.create_event(
                memoryId=self.memory_id,
                actorId=actor_id,
                sessionId=session_id,
                eventTimestamp=datetime.now(UTC),
                payload=[{"conversational": {"content": {"text": content},
                                              "role": agentcore_role}}],
            )
        except ClientError as e:
            raise self._translate(e) from e

        ev_data = resp["event"]
        return MemoryEvent(
            id=ev_data["eventId"],
            scope=scope,
            role=role,
            content=content,
            timestamp=ev_data["eventTimestamp"],
            metadata={"session_id": session_id, **(metadata or {})},
        )

    async def recall_facts(
        self, *, scope: str, query: str, top_k: int = 5,
    ) -> list[MemoryRecord]:
        return await self._retrieve(scope=scope, query=query, top_k=top_k,
                                    namespace_path="/")

    async def summarize_session(
        self, *, scope: str, session_id: str,
    ) -> MemoryRecord | None:
        # AgentCore returns one or more summary records; we return the most
        # recent. summarize_session is best-effort: if no SUMMARIZATION
        # strategy is configured on the memory, returns None.
        records = await self._retrieve(
            scope=scope, query=f"summary for session {session_id}",
            top_k=1, namespace_path=f"/summaries/{scope}/{session_id}",
        )
        return records[0] if records else None

    async def get_preferences(self, *, scope: str) -> list[MemoryRecord]:
        return await self._retrieve(
            scope=scope, query="user preferences", top_k=20,
            namespace_path=f"/users/{scope}/preferences",
        )

    async def list_events(self, *, scope: str, limit: int = 50) -> list[MemoryEvent]:
        actor_id = self._actor_id(scope)
        try:
            resp = self._client.list_events(
                memoryId=self.memory_id,
                actorId=actor_id,
                sessionId=self._default_session_id,
                maxResults=limit,
            )
        except ClientError as e:
            raise self._translate(e) from e

        events: list[MemoryEvent] = []
        for ev in resp.get("events", []):
            payload = ev.get("payload", [])
            # Best-effort content extraction
            text_parts = []
            role = "other"
            for p in payload:
                conv = p.get("conversational", {})
                if conv:
                    content = conv.get("content", {}).get("text", "")
                    text_parts.append(content)
                    role = conv.get("role", "other").lower()
            events.append(MemoryEvent(
                id=ev["eventId"],
                scope=scope,
                role=role,
                content="\n".join(text_parts),
                timestamp=ev.get("eventTimestamp", datetime.now(UTC)),
                metadata={"session_id": ev.get("sessionId", "")},
            ))
        return events

    async def clear(self, *, scope: str) -> int:
        events = await self.list_events(scope=scope, limit=1000)
        deleted = 0
        for ev in events:
            try:
                self._client.delete_event(
                    memoryId=self.memory_id,
                    actorId=self._actor_id(scope),
                    sessionId=ev.metadata.get("session_id") or self._default_session_id,
                    eventId=ev.id,
                )
                deleted += 1
            except ClientError:
                pass
        return deleted

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    async def _retrieve(
        self, *, scope: str, query: str, top_k: int, namespace_path: str,
    ) -> list[MemoryRecord]:
        try:
            resp = self._client.retrieve_memory_records(
                memoryId=self.memory_id,
                namespace=namespace_path,
                searchCriteria={"searchQuery": query, "topK": top_k},
            )
        except ClientError as e:
            raise self._translate(e) from e

        out: list[MemoryRecord] = []
        for r in resp.get("memoryRecordSummaries", []):
            out.append(MemoryRecord(
                id=r.get("memoryRecordId", ""),
                text=r.get("content", {}).get("text", ""),
                scope=scope,
                score=r.get("score"),
                metadata={"namespace": namespace_path,
                          "strategy_id": r.get("memoryStrategyId", "")},
            ))
        return out

    @staticmethod
    def _translate(e: ClientError) -> Exception:
        """Map boto3 errors to cloudless exception hierarchy (Q21)."""
        from cloudless.exceptions import (
            AuthenticationError,
            InvalidInputError,
            ThrottledError,
        )
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("ThrottlingException", "ServiceQuotaExceededException"):
            return ThrottledError(str(e))
        if code in ("AccessDeniedException", "UnauthorizedException"):
            return AuthenticationError(str(e))
        if code in ("ValidationException", "ResourceNotFoundException"):
            return InvalidInputError(str(e))
        return e
