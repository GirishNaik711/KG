"""Bedrock Knowledge Bases backend for cloudless.VectorStore.

Read-only — write path is via separate data source / sync flow which we
don't manage here. Per dossier 06: KB is the preferred AWS-side VectorStore
implementation, not raw OpenSearch.

API: `bedrock-agent-runtime:Retrieve`.
"""
from __future__ import annotations

import boto3
from botocore.exceptions import ClientError

from cloudless.catalog.vector_store import VectorMatch


class BedrockKBBackend:
    def __init__(self, *, kb_id: str, region: str = "us-east-1") -> None:
        self.kb_id = kb_id
        self._client = boto3.client("bedrock-agent-runtime", region_name=region)

    async def upsert(self, docs: list[dict]) -> int:
        raise NotImplementedError(
            "Bedrock Knowledge Bases upserts via data-source sync, not the "
            "Retrieve API. Configure your KB's data source separately."
        )

    async def search(self, *, query: str, top_k: int = 5) -> list[VectorMatch]:
        try:
            resp = self._client.retrieve(
                knowledgeBaseId=self.kb_id,
                retrievalQuery={"text": query},
                retrievalConfiguration={
                    "vectorSearchConfiguration": {"numberOfResults": top_k},
                },
            )
        except ClientError as e:
            raise self._translate(e) from e

        out: list[VectorMatch] = []
        for r in resp.get("retrievalResults", []):
            content = r.get("content", {})
            text = content.get("text", "")
            metadata = r.get("metadata") or {}
            location = r.get("location", {})
            out.append(VectorMatch(
                id=str(metadata.get("x-amz-bedrock-kb-source-uri", location)),
                text=text,
                score=float(r.get("score", 0.0)),
                metadata={"location": location, **{k: v for k, v in metadata.items() if isinstance(v, (str, int, float, bool))}},
            ))
        return out

    async def delete(self, *, ids: list[str]) -> int:
        raise NotImplementedError(
            "Bedrock Knowledge Bases doesn't expose per-document delete via API; "
            "manage deletions via your data-source backing store."
        )

    async def count(self) -> int:
        # KB doesn't expose a direct count; would require listing data
        # sources + their ingestion stats.
        return -1

    @staticmethod
    def _translate(e: ClientError) -> Exception:
        from cloudless.exceptions import (
            AuthenticationError,
            InvalidInputError,
            ThrottledError,
        )
        code = e.response.get("Error", {}).get("Code", "")
        if code == "ThrottlingException":
            return ThrottledError(str(e))
        if code in ("AccessDeniedException", "UnauthorizedException"):
            return AuthenticationError(str(e))
        if code in ("ValidationException", "ResourceNotFoundException"):
            return InvalidInputError(str(e))
        return e
