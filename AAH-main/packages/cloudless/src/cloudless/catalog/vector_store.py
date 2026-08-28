"""cloudless.VectorStore — semantic search over documents (Q9).

Backends:
  - InMemoryVectorBackend  — uses Embeddings + cosine similarity. For dev/tests.
  - BedrockKBBackend       — read-only RAG against a pre-provisioned Bedrock KB.

Future: Vertex AI RAG corpus (M2 GCP backend).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class VectorMatch:
    """One result from VectorStore.search."""

    id: str
    text: str
    score: float
    """Similarity (cosine for in-memory; provider-specific for managed)."""
    metadata: dict = field(default_factory=dict)


class VectorStoreBackend(Protocol):
    async def upsert(self, docs: list[dict]) -> int: ...
    async def search(self, *, query: str, top_k: int = 5) -> list[VectorMatch]: ...
    async def delete(self, *, ids: list[str]) -> int: ...
    async def count(self) -> int: ...


# --------------------------------------------------------------------- #
# InMemoryVectorBackend — uses cloudless.Embeddings + cosine similarity
# --------------------------------------------------------------------- #


class InMemoryVectorBackend:
    """No-persistence vector store for dev / tests.

    Computes embeddings via cloudless.Embeddings on upsert AND on search,
    then ranks via cosine similarity.
    """

    def __init__(self, *, embeddings) -> None:
        from cloudless.catalog.embeddings import Embeddings
        self._embeddings: Embeddings = embeddings
        self._docs: dict[str, dict] = {}
        # parallel storage for vectors keyed by doc id
        self._vectors: dict[str, list[float]] = {}

    async def upsert(self, docs: list[dict]) -> int:
        # docs is a list of {"id": str, "text": str, "metadata": {...}}
        texts = [d["text"] for d in docs]
        if not texts:
            return 0
        vectors = await self._embeddings.embed(texts)
        for d, v in zip(docs, vectors, strict=False):
            self._docs[d["id"]] = d
            self._vectors[d["id"]] = v
        return len(docs)

    async def search(self, *, query: str, top_k: int = 5) -> list[VectorMatch]:
        if not self._docs:
            return []
        qvec = (await self._embeddings.embed([query]))[0]
        scored = [
            (doc_id, _cosine(qvec, self._vectors[doc_id]))
            for doc_id in self._docs
        ]
        scored.sort(key=lambda kv: kv[1], reverse=True)
        out: list[VectorMatch] = []
        for doc_id, score in scored[:top_k]:
            d = self._docs[doc_id]
            out.append(VectorMatch(
                id=doc_id, text=d["text"], score=score,
                metadata=d.get("metadata") or {},
            ))
        return out

    async def delete(self, *, ids: list[str]) -> int:
        n = 0
        for i in ids:
            if i in self._docs:
                del self._docs[i]
                del self._vectors[i]
                n += 1
        return n

    async def count(self) -> int:
        return len(self._docs)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# --------------------------------------------------------------------- #
# User-facing VectorStore class
# --------------------------------------------------------------------- #


class VectorStore:
    """Unified vector store primitive (Q9).

    Example (in-memory + Bedrock Titan embeddings):
        import cloudless
        embed = cloudless.Embeddings(model="titan-v2")
        store = cloudless.VectorStore(embeddings=embed)
        await store.upsert([
            {"id": "1", "text": "Paris is the capital of France"},
            {"id": "2", "text": "Tokyo is the capital of Japan"},
        ])
        results = await store.search(query="Where is Tokyo?", top_k=2)
        # results[0].text → "Tokyo is the capital of Japan"

    Example (Bedrock Knowledge Bases read-only):
        store = cloudless.VectorStore(backend="bedrock-kb",
                                       kb_id="ABCD1234EF",
                                       region="us-east-1")
        results = await store.search(query="...", top_k=5)
    """

    def __init__(
        self,
        *,
        backend: str | VectorStoreBackend = "in_memory",
        embeddings=None,
        kb_id: str | None = None,
        region: str = "us-east-1",
        datastore_id: str | None = None,
        project: str | None = None,
        location: str = "global",
    ) -> None:
        if isinstance(backend, str):
            self._backend = self._build_backend(
                backend, embeddings=embeddings, kb_id=kb_id, region=region,
                datastore_id=datastore_id, project=project, location=location,
            )
        else:
            self._backend = backend

    @staticmethod
    def _build_backend(
        name: str, *, embeddings, kb_id: str | None, region: str,
        datastore_id: str | None = None,
        project: str | None = None,
        location: str = "global",
    ) -> VectorStoreBackend:
        if name == "in_memory":
            if embeddings is None:
                from cloudless.catalog.embeddings import Embeddings
                embeddings = Embeddings(model="titan-v2", region=region)
            return InMemoryVectorBackend(embeddings=embeddings)
        if name == "bedrock-kb":
            if not kb_id:
                raise ValueError("backend='bedrock-kb' requires kb_id")
            from cloudless.adapters.aws.vector_store import BedrockKBBackend
            return BedrockKBBackend(kb_id=kb_id, region=region)
        if name == "vertex-search":
            if not datastore_id:
                raise ValueError("backend='vertex-search' requires datastore_id")
            from cloudless.adapters.gcp.vector_store import VertexSearchBackend
            return VertexSearchBackend(
                datastore_id=datastore_id, project=project, location=location,
            )
        raise ValueError(f"Unknown VectorStore backend: {name!r}")

    async def upsert(self, docs: list[dict]) -> int:
        return await self._backend.upsert(docs)

    async def search(self, *, query: str, top_k: int = 5) -> list[VectorMatch]:
        return await self._backend.search(query=query, top_k=top_k)

    async def delete(self, *, ids: list[str]) -> int:
        return await self._backend.delete(ids=ids)

    async def count(self) -> int:
        return await self._backend.count()
