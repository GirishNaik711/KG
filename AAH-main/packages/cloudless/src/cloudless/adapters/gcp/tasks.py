"""Vertex Memory Bank-backed TaskStore for HITL pause/resume on GCP.

Persists `TaskRecord` instances as Memory Bank "fact" entries under a
dedicated scope `{"cloudless_task_token": "<resume_token>"}`. Each
pause writes one CreateMemory call with the JSON-encoded record as the
fact body. Resume() walks the same scope and writes an updated record.

We use the simple-retrieval path (filter by scope) rather than semantic
similarity — task lookup is exact-match on resume_token.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from cloudless.runtime.tasks import TaskRecord

SCOPE_KEY = "cloudless_task_token"


class MemoryBankTaskStore:
    """TaskStore that persists into a Vertex Memory Bank.

    Uses the v1beta1 MemoryBankServiceClient. Each TaskRecord is one
    Memory whose `fact` is the JSON-encoded record and whose `scope`
    is keyed by the resume_token (so lookups are O(1)).
    """

    def __init__(
        self,
        *,
        agent_engine_name: str,
        location: str = "us-central1",
        client: Any = None,
    ) -> None:
        self.agent_engine_name = agent_engine_name
        self.location = location
        if client is not None:
            self._client = client
        else:
            from google.cloud import aiplatform_v1beta1 as v1b
            endpoint = f"{location}-aiplatform.googleapis.com"
            self._client = v1b.MemoryBankServiceClient(
                client_options={"api_endpoint": endpoint},
            )

    # ------------------------------------------------------------------ #
    # Encoding helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _encode(rec: TaskRecord) -> str:
        return json.dumps(asdict(rec), default=str)

    @staticmethod
    def _decode(blob: str) -> TaskRecord:
        data = json.loads(blob)
        return TaskRecord(**data)

    def _scope(self, resume_token: str) -> dict[str, str]:
        return {SCOPE_KEY: resume_token}

    # ------------------------------------------------------------------ #
    # TaskStore Protocol
    # ------------------------------------------------------------------ #

    def put(self, record: TaskRecord) -> None:
        from google.cloud import aiplatform_v1beta1 as v1b
        memory = v1b.Memory(
            fact=self._encode(record),
            scope=self._scope(record.resume_token),
        )
        request = v1b.CreateMemoryRequest(parent=self.agent_engine_name, memory=memory)
        # CreateMemory is an LRO; we don't need to wait — the next read
        # will pick up the latest fact (replay-safe).
        self._client.create_memory(request=request)

    def get(self, resume_token: str) -> TaskRecord | None:
        from google.cloud import aiplatform_v1beta1 as v1b
        filter_str = f'scope.{SCOPE_KEY}="{resume_token}"'
        request = v1b.ListMemoriesRequest(parent=self.agent_engine_name, filter=filter_str)
        latest: TaskRecord | None = None
        latest_time = None
        for mem in self._client.list_memories(request=request):
            try:
                rec = self._decode(mem.fact)
            except Exception:
                continue
            create_time = getattr(mem, "create_time", None)
            if latest is None or (create_time and (latest_time is None or create_time > latest_time)):
                latest = rec
                latest_time = create_time
        return latest

    def resolve(self, resume_token: str, approval: dict) -> TaskRecord | None:
        rec = self.get(resume_token)
        if rec is None or rec.resolved:
            return None
        rec.resolved = True
        rec.approval = approval
        self.put(rec)
        return rec

    def delete(self, resume_token: str) -> None:
        # Mark deleted via scope filter — Memory Bank supports DeleteMemory.
        from google.cloud import aiplatform_v1beta1 as v1b
        filter_str = f'scope.{SCOPE_KEY}="{resume_token}"'
        request = v1b.ListMemoriesRequest(parent=self.agent_engine_name, filter=filter_str)
        for mem in self._client.list_memories(request=request):
            try:
                self._client.delete_memory(name=mem.name)
            except Exception:
                pass

    def list_active(self) -> list[TaskRecord]:
        import time as _t

        from google.cloud import aiplatform_v1beta1 as v1b
        # All memories under our parent with scope key set.
        # Memory Bank's filter syntax doesn't easily support "key exists",
        # so we list and filter client-side.
        request = v1b.ListMemoriesRequest(parent=self.agent_engine_name)
        records: dict[str, TaskRecord] = {}
        for mem in self._client.list_memories(request=request):
            scope = dict(mem.scope) if mem.scope else {}
            if SCOPE_KEY not in scope:
                continue
            try:
                rec = self._decode(mem.fact)
            except Exception:
                continue
            records[rec.resume_token] = rec
        now = _t.time()
        return [r for r in records.values() if not r.resolved and r.expires_at > now]
