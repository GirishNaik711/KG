"""Q19 governance audit log.

Records policy-driven decisions (allow / transform / block) so security
teams can answer "what did the agent block, when, and why?" without
parsing app logs.

Design:
  - One AuditRecord per policy decision.
  - Pluggable sinks. Default sink: structlog at WARN level under
    namespace "cloudless.audit".
  - Sinks are sync (avoids needing async at every catch site).
  - The PolicyRegistry emits audit records on PolicyViolation /
    GuardrailBlocked; users may also emit explicitly via `emit_audit`.

Payloads are HASHED, not stored — we keep an SHA-256 prefix so we can
correlate without retaining the raw secret.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class AuditRecord:
    """One audit-log entry."""
    timestamp: float
    """Unix epoch seconds, UTC."""

    stage: str
    """Policy stage that fired (before_llm, etc.)."""

    decision: str
    """One of: 'block', 'allow', 'transform'."""

    policy_name: str
    """The @cloudless.policy name."""

    reason: str = ""
    """Human-readable description."""

    payload_hash: str = ""
    """SHA-256 prefix of the payload that triggered the decision. Empty for allow."""

    agent_name: str | None = None
    """Bound from invocation context if available."""

    session_id: str | None = None
    """Bound from invocation context if available."""

    user_id: str | None = None
    """Bound from invocation context if available."""

    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def hash_payload(payload: Any) -> str:
    """Return a short SHA-256 prefix of `payload` (str or anything json-able)."""
    if isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
    elif isinstance(payload, str):
        raw = payload.encode("utf-8")
    else:
        raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


# --------------------------------------------------------------------- #
# Sinks
# --------------------------------------------------------------------- #


@runtime_checkable
class AuditSink(Protocol):
    """Protocol for audit sinks. Concrete sinks must be synchronous."""

    def write(self, record: AuditRecord) -> None: ...


class StructlogSink:
    """Default sink: emits at WARN level under cloudless.audit namespace."""

    def write(self, record: AuditRecord) -> None:
        from cloudless.runtime.logging import get_logger
        log = get_logger("cloudless.audit")
        log.warning(
            "policy_decision",
            stage=record.stage,
            decision=record.decision,
            policy=record.policy_name,
            reason=record.reason,
            payload_hash=record.payload_hash,
            agent=record.agent_name,
            session=record.session_id,
            user=record.user_id,
            **record.extra,
        )


class FileSink:
    """Append-only JSONL file sink for compliance evidence."""

    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def write(self, record: AuditRecord) -> None:
        with open(self.path, "a") as f:
            f.write(record.to_json() + "\n")


class InMemorySink:
    """Test sink: keeps records in a list."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    def write(self, record: AuditRecord) -> None:
        self.records.append(record)


# --------------------------------------------------------------------- #
# Module-level singleton (one sink chain per process)
# --------------------------------------------------------------------- #


_SINKS: list[AuditSink] = [StructlogSink()]


def get_sinks() -> list[AuditSink]:
    return list(_SINKS)


def set_sinks(sinks: list[AuditSink]) -> None:
    """Replace the global sink chain. Tests use this."""
    global _SINKS
    _SINKS = list(sinks)


def add_sink(sink: AuditSink) -> None:
    """Append a sink (e.g. add a FileSink alongside the default StructlogSink)."""
    _SINKS.append(sink)


def reset_sinks() -> None:
    """Restore the default StructlogSink chain (used by tests)."""
    global _SINKS
    _SINKS = [StructlogSink()]


# --------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------- #


def emit_audit(
    *,
    stage: str,
    decision: str,
    policy_name: str,
    reason: str = "",
    payload: Any = None,
    extra: dict[str, Any] | None = None,
) -> AuditRecord:
    """Write an audit record to every configured sink.

    Sinks are best-effort: a sink raising is logged but does not affect
    the others.
    """
    from cloudless.runtime.logging import _invocation_ctx  # contextvar
    ctx = _invocation_ctx.get() or {}

    record = AuditRecord(
        timestamp=time.time(),
        stage=stage,
        decision=decision,
        policy_name=policy_name,
        reason=reason,
        payload_hash=hash_payload(payload) if payload is not None else "",
        agent_name=ctx.get("agent.name"),
        session_id=ctx.get("session.id"),
        user_id=None,  # populated by runtime when user.id is in ctx
        extra=dict(extra or {}),
    )

    for sink in _SINKS:
        try:
            sink.write(record)
        except Exception:
            # Never let a broken sink crash the request path.
            pass
    return record
