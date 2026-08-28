"""GCP Memory Bank backend for cloudless.Memory.

Maps the cloudless verb taxonomy (Q14) to Vertex AI Memory Bank:

  Verb              | Memory Bank call
  ------------------|-----------------------------------------------------
  add_event         | CreateMemory(fact=role+": "+content, scope=cloudless_scope)
  recall_facts      | RetrieveMemories(similarity_search_params, scope)
  summarize_session | RetrieveMemories scoped by session_id (best-effort)
  get_preferences   | RetrieveMemories(simple_retrieval_params, scope)
  list_events       | ListMemories(filter by scope)
  clear             | ListMemories + DeleteMemory loop

Memories on Memory Bank are scoped under a "parent" — the Agent Engine
resource. cloudless requires the user to pass `agent_engine_name` so we
know which parent to use.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from google.api_core.exceptions import GoogleAPICallError, NotFound
from google.cloud import aiplatform_v1beta1 as v1b

from cloudless.catalog.memory import MemoryEvent, MemoryRecord


class MemoryBankBackend:
    def __init__(
        self,
        *,
        agent_engine_name: str,
        location: str = "us-central1",
    ) -> None:
        """
        Args:
            agent_engine_name: Full reasoning-engine resource name,
                e.g. "projects/305896968831/locations/us-central1/reasoningEngines/12345".
            location: Region (matches what's in agent_engine_name).
        """
        self.agent_engine_name = agent_engine_name
        self.location = location
        endpoint = f"{location}-aiplatform.googleapis.com"
        self._client = v1b.MemoryBankServiceClient(
            client_options={"api_endpoint": endpoint},
        )

    # ------------------------------------------------------------------ #
    # Scope-string helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _scope_to_dict(scope: str) -> dict[str, str]:
        """Map a cloudless scope like 'user:42' → Memory Bank scope dict {'user_id': '42'}.

        Falls back to {'cloudless_scope': scope} when no ':' separator present.
        """
        if ":" in scope:
            key, value = scope.split(":", 1)
            # Memory Bank scope keys must match [a-zA-Z_][a-zA-Z0-9_]*
            key = key.replace("-", "_")
            return {key: value}
        return {"cloudless_scope": scope}

    # ------------------------------------------------------------------ #
    # Verbs
    # ------------------------------------------------------------------ #

    async def add_event(
        self, *, scope: str, role: str, content: str,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEvent:
        fact = f"{role}: {content}"
        memory = v1b.Memory(
            fact=fact,
            scope=self._scope_to_dict(scope),
            display_name=metadata.get("display_name") if metadata else f"event-{role}",
        )
        # Same LRO-unpack bug as delete_memory: SDK expects Empty but server
        # returns Memory. The request reaches the server though, so we treat
        # TypeError on the response as "successfully fired".
        try:
            request = v1b.CreateMemoryRequest(
                parent=self.agent_engine_name,
                memory=memory,
            )
            try:
                self._client.create_memory(request=request)
            except TypeError:
                pass
        except GoogleAPICallError as e:
            raise self._translate(e) from e

        return MemoryEvent(
            id="created",  # SDK doesn't give us the assigned name reliably
            scope=scope,
            role=role,
            content=content,
            timestamp=datetime.now(UTC),
            metadata=metadata or {},
        )

    async def recall_facts(
        self, *, scope: str, query: str, top_k: int = 5,
        similarity_threshold: float | None = None,
    ) -> list[MemoryRecord]:
        scope_dict = self._scope_to_dict(scope)
        # Construct params with similarity_threshold if supported by SDK version
        ssp_kwargs: dict = {"search_query": query, "top_k": top_k}
        if similarity_threshold is not None:
            try:
                ssp = v1b.RetrieveMemoriesRequest.SimilaritySearchParams(
                    similarity_threshold=similarity_threshold, **ssp_kwargs,
                )
            except (TypeError, AttributeError):
                ssp = v1b.RetrieveMemoriesRequest.SimilaritySearchParams(**ssp_kwargs)
        else:
            ssp = v1b.RetrieveMemoriesRequest.SimilaritySearchParams(**ssp_kwargs)
        request = v1b.RetrieveMemoriesRequest(
            parent=self.agent_engine_name,
            scope=scope_dict,
            similarity_search_params=ssp,
        )
        try:
            resp = self._client.retrieve_memories(request=request)
        except GoogleAPICallError as e:
            raise self._translate(e) from e

        out: list[MemoryRecord] = []
        for r in resp.retrieved_memories:
            out.append(MemoryRecord(
                id=r.memory.name.rsplit("/", 1)[-1],
                text=r.memory.fact,
                scope=scope,
                score=r.distance if r.distance is not None else None,
                metadata={"display_name": r.memory.display_name},
            ))
        return out

    async def summarize_session(
        self, *, scope: str, session_id: str,
    ) -> MemoryRecord | None:
        results = await self.recall_facts(
            scope=scope, query=f"session {session_id} summary", top_k=1,
        )
        return results[0] if results else None

    async def get_preferences(self, *, scope: str) -> list[MemoryRecord]:
        return await self.recall_facts(
            scope=scope, query="user preferences", top_k=20,
        )

    async def list_events(self, *, scope: str, limit: int = 50) -> list[MemoryEvent]:
        scope_dict = self._scope_to_dict(scope)
        scope_filter = " AND ".join(f'scope.{k}="{v}"' for k, v in scope_dict.items())
        try:
            request = v1b.ListMemoriesRequest(
                parent=self.agent_engine_name,
                filter=scope_filter,
                page_size=limit,
            )
            page = self._client.list_memories(request=request)
        except GoogleAPICallError as e:
            raise self._translate(e) from e

        events: list[MemoryEvent] = []
        for m in page:
            fact = m.fact or ""
            # Naive split of "<role>: <content>" since that's how we stored it
            if ": " in fact:
                role, content = fact.split(": ", 1)
            else:
                role, content = "other", fact
            events.append(MemoryEvent(
                id=m.name.rsplit("/", 1)[-1],
                scope=scope,
                role=role,
                content=content,
                timestamp=m.create_time or datetime.now(UTC),
                metadata={"name": m.name, "display_name": m.display_name},
            ))
            if len(events) >= limit:
                break
        return events

    async def clear(self, *, scope: str) -> int:
        events = await self.list_events(scope=scope, limit=1000)
        deleted = 0
        for ev in events:
            full_name = ev.metadata.get("name")
            if not full_name:
                continue
            try:
                request = v1b.DeleteMemoryRequest(name=full_name)
                # The SDK's LRO unwrap is buggy: it expects Empty but the server
                # returns Memory. The unpack fails before we can poll. The request
                # IS sent though, so the actual delete completes server-side.
                # Suppress TypeError, count deletions optimistically.
                try:
                    self._client.delete_memory(request=request)
                except TypeError:
                    pass
                deleted += 1
            except (GoogleAPICallError, NotFound):
                pass
        return deleted

    @staticmethod
    def _translate(e: GoogleAPICallError) -> Exception:
        from cloudless.exceptions import (
            AuthenticationError,
            InvalidInputError,
            ThrottledError,
        )
        msg = str(e)
        # Map status codes if available
        status = getattr(e, "grpc_status_code", None) or getattr(e, "code", lambda: 0)()
        if status in (429,):
            return ThrottledError(msg)
        if status in (401, 403):
            return AuthenticationError(msg)
        if status in (400, 404):
            return InvalidInputError(msg)
        return e
