"""Vertex AI Embeddings backend using google-genai (vertex mode).

Models: text-embedding-005, text-embedding-004, text-multilingual-embedding-002,
gemini-embedding-001. The google-genai SDK exposes them via
`client.models.embed_content`.

Supports:
  - task_type: RETRIEVAL_QUERY, RETRIEVAL_DOCUMENT, SEMANTIC_SIMILARITY,
    CLASSIFICATION, CLUSTERING, QUESTION_ANSWERING, FACT_VERIFICATION,
    CODE_RETRIEVAL_QUERY
  - output_dimensionality: force a smaller vector (for storage cost)
"""
from __future__ import annotations

from typing import Any


class VertexEmbeddingsBackend:
    def __init__(
        self,
        *,
        model_id: str,
        project: str,
        location: str = "us-central1",
        task_type: str | None = None,
        output_dimensionality: int | None = None,
    ) -> None:
        from google import genai
        self.model_id = model_id
        self.project = project
        self.location = location
        self.task_type = task_type
        self.output_dimensionality = output_dimensionality
        self._client = genai.Client(vertexai=True, project=project, location=location)

    async def embed(self, texts: list[str], *, task_type: str | None = None) -> list[list[float]]:
        config: dict[str, Any] = {}
        # Per-call task_type overrides constructor-level default
        effective_task = task_type or self.task_type
        if effective_task:
            config["task_type"] = effective_task
        if self.output_dimensionality:
            config["output_dimensionality"] = self.output_dimensionality

        try:
            kwargs: dict[str, Any] = {"model": self.model_id, "contents": texts}
            if config:
                kwargs["config"] = config
            resp = self._client.models.embed_content(**kwargs)
        except Exception as e:
            raise self._translate(e) from e

        result: list[list[float]] = []
        embeddings = getattr(resp, "embeddings", None)
        if embeddings is None:
            raise RuntimeError(f"unexpected embed_content response: {resp!r}")
        for emb in embeddings:
            values = getattr(emb, "values", None)
            if values is None and isinstance(emb, dict):
                values = emb.get("values")
            if values is None:
                raise RuntimeError(f"embedding missing .values: {emb!r}")
            result.append(list(values))
        return result

    @staticmethod
    def _translate(e: Exception) -> Exception:
        from cloudless.adapters.gcp.llm import GeminiBackend
        return GeminiBackend._translate(e)
