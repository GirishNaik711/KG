"""Vertex AI Search backend for cloudless.VectorStore.

Read-only access to a pre-provisioned Vertex AI Search datastore (Discovery
Engine). Provisioning a datastore is a multi-step GCP setup; cloudless
doesn't manage the lifecycle. Users plug in a datastore ID.

Vertex Search supports two query modes:
  - `searchService.search` — keyword / hybrid search
  - `searchService.search` with `summarySpec` — RAG-style with answer

Cloudless wraps the search endpoint and surfaces results as VectorMatch.
"""
from __future__ import annotations

from typing import Any

from cloudless.catalog.vector_store import VectorMatch


class VertexSearchBackend:
    """Read-only Vertex AI Search (Discovery Engine) backend.

    Args:
        datastore_id: Full Discovery Engine datastore resource path:
            `projects/<num>/locations/<location>/collections/default_collection/dataStores/<id>`
        project: GCP project (defaults to env / ADC).
        location: GCP region. Vertex Search is `global` for most datastores;
            otherwise `us` or `eu`.
        serving_config_id: Serving config under the datastore (default `default_config`).
    """

    def __init__(
        self,
        *,
        datastore_id: str,
        project: str | None = None,
        location: str = "global",
        serving_config_id: str = "default_config",
        client: Any = None,
    ) -> None:
        self.datastore_id = datastore_id
        self.project = project
        self.location = location
        self.serving_config_id = serving_config_id

        if client is not None:
            self._client = client
        else:
            try:
                from google.cloud import discoveryengine_v1
            except ImportError as e:
                raise RuntimeError(
                    "google-cloud-discoveryengine is required for VertexSearchBackend. "
                    "Install with: pip install google-cloud-discoveryengine"
                ) from e
            self._client = discoveryengine_v1.SearchServiceClient(
                client_options={"api_endpoint": f"{location}-discoveryengine.googleapis.com"}
                if location != "global"
                else None,
            )

    def _serving_config_path(self) -> str:
        return (
            f"{self.datastore_id}/servingConfigs/{self.serving_config_id}"
        )

    async def upsert(self, docs: list[dict]) -> int:
        # Vertex Search ingestion is managed via Discovery Engine's document
        # import flow (S3/GCS/BQ/JSON). cloudless doesn't bundle that.
        raise NotImplementedError(
            "VertexSearchBackend is read-only. Ingest documents via the "
            "Discovery Engine API or console."
        )

    async def search(self, *, query: str, top_k: int = 5) -> list[VectorMatch]:
        try:
            from google.cloud import discoveryengine_v1
            request = discoveryengine_v1.SearchRequest(
                serving_config=self._serving_config_path(),
                query=query,
                page_size=top_k,
            )
        except ImportError:
            # SDK not installed but a stub client was passed (test path)
            class _StubReq:
                def __init__(self, **kw):
                    for k, v in kw.items():
                        setattr(self, k, v)
            request = _StubReq(
                serving_config=self._serving_config_path(),
                query=query,
                page_size=top_k,
            )
        try:
            page = self._client.search(request=request)
        except Exception as e:
            raise self._translate(e) from e

        out: list[VectorMatch] = []
        for r in page.results:
            doc = r.document
            text = ""
            if doc.derived_struct_data:
                text = doc.derived_struct_data.get("title", "") or ""
            # Vertex Search doesn't always return per-result scores; default to 0
            out.append(VectorMatch(
                id=doc.id, text=text, score=0.0,
                metadata={"name": doc.name, "uri": doc.derived_struct_data.get("uri", "") if doc.derived_struct_data else ""},
            ))
            if len(out) >= top_k:
                break
        return out

    async def delete(self, *, ids: list[str]) -> int:
        raise NotImplementedError(
            "VertexSearchBackend is read-only. Delete via Discovery Engine docs API."
        )

    async def count(self) -> int:
        # No cheap count API; users should query their dataset directly
        raise NotImplementedError(
            "Vertex Search does not expose a count API; query directly."
        )

    @staticmethod
    def _translate(e: Exception) -> Exception:
        from cloudless.adapters.gcp.llm import GeminiBackend
        return GeminiBackend._translate(e)
