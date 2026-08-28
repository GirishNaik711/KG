"""cloudless runtime — the embedded library every deployed agent imports.

Provides:
  - `Context`: per-invocation context passed to `agent.query(ctx, prompt)`
  - `Session`: stable session identifier (maps to AgentCore microVM session ID
    on AWS, custom session ID on GCP)
  - `CostTracker`: per-invocation cost accumulator (Q20)
  - `PeerClient`: A2A peer caller (Q12 + Q7)

The runtime lib is intentionally framework-agnostic. Framework adapters
(cloudless.adapters.frameworks.*) plumb framework-native concepts into
this runtime contract.
"""
from __future__ import annotations

from cloudless.runtime import tracing
from cloudless.runtime.a2a_server import build_a2a_app
from cloudless.runtime.audit import (
    AuditRecord,
    AuditSink,
    FileSink,
    InMemorySink,
    StructlogSink,
    emit_audit,
)
from cloudless.runtime.audit import (
    add_sink as add_audit_sink,
)
from cloudless.runtime.audit import (
    get_sinks as get_audit_sinks,
)
from cloudless.runtime.audit import (
    reset_sinks as reset_audit_sinks,
)
from cloudless.runtime.audit import (
    set_sinks as set_audit_sinks,
)
from cloudless.runtime.context import (
    Context,
    CostTracker,
    InMemoryContext,
    PeerClient,
    Session,
    User,
)
from cloudless.runtime.cost_sinks import (
    CostRecord,
    CostSink,
    InMemoryCostSink,
    JsonlCostSink,
    add_cost_sink,
    emit_cost,
    get_cost_sinks,
    reset_cost_sinks,
    set_cost_sinks,
)
from cloudless.runtime.identity import (
    InMemoryTokenStore,
    OAuth3LOConfig,
    OAuth3LOIdentity,
    OAuthRequired,
    SigV4Identity,
    StoredToken,
    TokenStore,
)
from cloudless.runtime.logging import (
    add_redact_pattern,
    bind_invocation,
    clear_invocation,
    configure,
    get_logger,
)
from cloudless.runtime.manifest import (
    Manifest,
    ManifestRefresher,
    PeerEntry,
    load_manifest,
)
from cloudless.runtime.peer import A2APeerClient, CognitoIdentity, build_peer_client
from cloudless.runtime.policy import (
    VALID_STAGES,
    PolicyEntry,
    PolicyRegistry,
    policy,
)
from cloudless.runtime.policy import (
    get_registry as get_policy_registry,
)
from cloudless.runtime.resilience import (
    CircuitBreaker,
    get_breaker,
    reset_breakers,
    resilient,
    with_retry,
    with_timeout,
)
from cloudless.runtime.tasks import (
    InMemoryTaskStore,
    TaskRecord,
    TaskStore,
    get_task,
    list_active_tasks,
    pause,
    resume,
)
from cloudless.runtime.tasks import (
    get_store as get_task_store,
)
from cloudless.runtime.tasks import (
    reset_store as reset_task_store,
)
from cloudless.runtime.tasks import (
    set_store as set_task_store,
)

__all__ = [
    "VALID_STAGES",
    "A2APeerClient",
    "AuditRecord",
    "AuditSink",
    "CircuitBreaker",
    "CognitoIdentity",
    "Context",
    "CostRecord",
    "CostSink",
    "CostTracker",
    "FileSink",
    "InMemoryContext",
    "InMemoryCostSink",
    "InMemorySink",
    "InMemoryTaskStore",
    "InMemoryTokenStore",
    "JsonlCostSink",
    "Manifest",
    "ManifestRefresher",
    "OAuth3LOConfig",
    "OAuth3LOIdentity",
    "OAuthRequired",
    "PeerClient",
    "PeerEntry",
    "PolicyEntry",
    "PolicyRegistry",
    "Session",
    "SigV4Identity",
    "StoredToken",
    "StructlogSink",
    "TaskRecord",
    "TaskStore",
    "TokenStore",
    "User",
    "add_audit_sink",
    "add_cost_sink",
    "add_redact_pattern",
    "bind_invocation",
    "build_a2a_app",
    "build_peer_client",
    "clear_invocation",
    "configure",
    "emit_audit",
    "emit_cost",
    "get_audit_sinks",
    "get_breaker",
    "get_cost_sinks",
    "get_logger",
    "get_policy_registry",
    "get_task",
    "get_task_store",
    "list_active_tasks",
    "load_manifest",
    "pause",
    "policy",
    "reset_audit_sinks",
    "reset_breakers",
    "reset_cost_sinks",
    "reset_task_store",
    "resilient",
    "resume",
    "set_audit_sinks",
    "set_cost_sinks",
    "set_task_store",
    "tracing",
    "with_retry",
    "with_timeout",
]
