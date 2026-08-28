"""cloudless service catalog — unified primitives over AWS + GCP services.

Per Q9, v1 catalog covers 11 primitives. This module exposes the user-
facing ones; each maps to a cloud-native implementation in
cloudless.adapters.aws.* / cloudless.adapters.gcp.*.

M1 ships LLM only (AWS-only); subsequent milestones add the rest:
  - Memory (M1 — AgentCore Memory backend)
  - Secrets (M1 — Secrets Manager backend)
  - VectorStore (M3 — Knowledge Bases preferred over raw OpenSearch per dossier 06)
  - Embeddings, Sandbox, Tools/Gateway, A2A, Observability, IdentityVault, Browser (M2-M5)
"""
from __future__ import annotations

from cloudless.catalog.embeddings import (
    DEFAULT_EMBEDDING_ALIASES,
    EmbeddingAlias,
    Embeddings,
    EmbeddingsBackend,
    resolve_embedding,
)
from cloudless.catalog.llm import LLM, ModelAlias, resolve_model
from cloudless.catalog.memory import (
    InMemoryBackend,
    Memory,
    MemoryBackend,
    MemoryEvent,
    MemoryRecord,
)
from cloudless.catalog.sandbox import (
    LocalSubprocessBackend,
    Sandbox,
    SandboxBackend,
    SandboxResult,
)
from cloudless.catalog.secrets import LocalFileBackend, Secrets, SecretsBackend
from cloudless.catalog.tools import Tool, tool
from cloudless.catalog.vector_store import (
    InMemoryVectorBackend,
    VectorMatch,
    VectorStore,
    VectorStoreBackend,
)

__all__ = [
    "DEFAULT_EMBEDDING_ALIASES",
    "LLM",
    "EmbeddingAlias",
    "Embeddings",
    "EmbeddingsBackend",
    "InMemoryBackend",
    "InMemoryVectorBackend",
    "LocalFileBackend",
    "LocalSubprocessBackend",
    "Memory",
    "MemoryBackend",
    "MemoryEvent",
    "MemoryRecord",
    "ModelAlias",
    "Sandbox",
    "SandboxBackend",
    "SandboxResult",
    "Secrets",
    "SecretsBackend",
    "Tool",
    "VectorMatch",
    "VectorStore",
    "VectorStoreBackend",
    "resolve_embedding",
    "resolve_model",
    "tool",
]
