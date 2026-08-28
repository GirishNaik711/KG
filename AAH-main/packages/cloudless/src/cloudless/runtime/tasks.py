"""Q17 HITL + long-running task store.

When an agent yields a `PauseChunk`, the embedded runtime persists the
task state (session_id, agent_name, pending_action) keyed by the
resume_token. A subsequent call to `resume(resume_token, approval)` looks
up the state and re-invokes the agent with the human's decision.

Two store backends:
  - InMemoryTaskStore: cloudless dev + tests
  - AgentCoreTaskStore: deploys on AWS — persists into AgentCore Memory
                       under a `cloudless/tasks/<token>` event tag
  - MemoryBankTaskStore: deploys on GCP — persists into Memory Bank

This module ships InMemoryTaskStore + abstract Protocol. Concrete cloud
backends live in cloudless.adapters.aws.tasks / .gcp.tasks (M3+).
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class TaskRecord:
    """Persisted state for a paused agent invocation."""
    resume_token: str
    agent_name: str
    session_id: str
    reason: str
    pending_action: dict | None = None
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 86400)
    """Default TTL: 24h."""
    state: dict[str, Any] = field(default_factory=dict)
    """Arbitrary agent-supplied state. The runtime echoes it back on resume."""
    resolved: bool = False
    """Set to True when resume() succeeds; prevents double-resume."""
    approval: dict | None = None
    """Populated by resume() with the human's decision."""


class TaskStore(Protocol):
    """Sync store contract — runtime adapters supply concrete backends."""
    def put(self, record: TaskRecord) -> None: ...
    def get(self, resume_token: str) -> TaskRecord | None: ...
    def resolve(self, resume_token: str, approval: dict) -> TaskRecord | None: ...
    def delete(self, resume_token: str) -> None: ...
    def list_active(self) -> list[TaskRecord]: ...


class InMemoryTaskStore:
    """Process-local in-memory store. cloudless dev + unit tests."""

    def __init__(self) -> None:
        self._records: dict[str, TaskRecord] = {}

    def put(self, record: TaskRecord) -> None:
        self._records[record.resume_token] = record

    def get(self, resume_token: str) -> TaskRecord | None:
        rec = self._records.get(resume_token)
        if rec is None:
            return None
        if rec.expires_at < time.time():
            self.delete(resume_token)
            return None
        return rec

    def resolve(self, resume_token: str, approval: dict) -> TaskRecord | None:
        rec = self.get(resume_token)
        if rec is None:
            return None
        if rec.resolved:
            return None  # idempotent — second resume is a no-op
        rec.resolved = True
        rec.approval = approval
        return rec

    def delete(self, resume_token: str) -> None:
        self._records.pop(resume_token, None)

    def list_active(self) -> list[TaskRecord]:
        now = time.time()
        return [r for r in self._records.values()
                if not r.resolved and r.expires_at > now]


# --------------------------------------------------------------------- #
# Module-level singleton (process-scoped). Tests reset via reset_store.
# --------------------------------------------------------------------- #


_STORE: TaskStore = InMemoryTaskStore()


def get_store() -> TaskStore:
    return _STORE


def set_store(store: TaskStore) -> None:
    """Replace the global store. Cloud adapters use this at runtime init."""
    global _STORE
    _STORE = store


def reset_store() -> None:
    """Restore the default InMemoryTaskStore. Test helper."""
    global _STORE
    _STORE = InMemoryTaskStore()


# --------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------- #


def new_resume_token() -> str:
    """Generate a fresh resume_token. URL-safe, 32 chars."""
    return secrets.token_urlsafe(24)


def pause(
    *,
    agent_name: str,
    session_id: str,
    reason: str = "",
    pending_action: dict | None = None,
    state: dict | None = None,
    ttl_seconds: float = 86400.0,
) -> TaskRecord:
    """Helper for agents to create a paused task.

    Usage in an agent:

        from cloudless.chunks import PauseChunk
        from cloudless.runtime.tasks import pause

        rec = pause(
            agent_name=self.name, session_id=ctx.session.id,
            reason="refund > $1000 needs approval",
            pending_action={"refund_usd": amount, "order_id": "o123"},
        )
        yield PauseChunk(
            resume_token=rec.resume_token,
            reason=rec.reason,
            pending_action=rec.pending_action,
            expires_at=rec.expires_at,
        )
        return  # The runtime persists rec; resume() will re-invoke.
    """
    rec = TaskRecord(
        resume_token=new_resume_token(),
        agent_name=agent_name,
        session_id=session_id,
        reason=reason,
        pending_action=pending_action,
        expires_at=time.time() + ttl_seconds,
        state=dict(state or {}),
    )
    _STORE.put(rec)
    return rec


def resume(resume_token: str, approval: dict) -> TaskRecord | None:
    """Mark a paused task as resolved. Returns the record, or None if expired/unknown.

    The actual agent re-invocation is the runtime's job — this function only
    persists the human's decision. After this returns, the runtime should
    locate `record.agent_name`, instantiate it, set ctx to the saved
    `session_id`, and re-run the agent with the approval as input.
    """
    return _STORE.resolve(resume_token, approval)


def get_task(resume_token: str) -> TaskRecord | None:
    return _STORE.get(resume_token)


def list_active_tasks() -> list[TaskRecord]:
    return _STORE.list_active()
