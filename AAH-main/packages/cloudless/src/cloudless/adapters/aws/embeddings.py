"""Bedrock Embeddings backend."""
from __future__ import annotations

import json

import boto3
from botocore.exceptions import ClientError


class BedrockEmbeddingsBackend:
    def __init__(self, *, model_id: str, region: str = "us-east-1") -> None:
        self.model_id = model_id
        self._client = boto3.client("bedrock-runtime", region_name=region)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        # Bedrock embed API shape differs by provider:
        #   Titan v2: {"inputText": "..."} → single vector per call
        #   Cohere v3: {"texts": [...], "input_type": "search_document"} → batch
        is_cohere = self.model_id.startswith("cohere.")

        vectors: list[list[float]] = []
        if is_cohere:
            body = {"texts": texts, "input_type": "search_document"}
            try:
                resp = self._client.invoke_model(
                    modelId=self.model_id, body=json.dumps(body).encode(),
                )
            except ClientError as e:
                raise self._translate(e) from e
            payload = json.loads(resp["body"].read())
            vectors = payload.get("embeddings", [])
        else:
            # Titan: one call per text
            for text in texts:
                body = {"inputText": text}
                try:
                    resp = self._client.invoke_model(
                        modelId=self.model_id, body=json.dumps(body).encode(),
                    )
                except ClientError as e:
                    raise self._translate(e) from e
                payload = json.loads(resp["body"].read())
                vectors.append(payload["embedding"])
        return vectors

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
