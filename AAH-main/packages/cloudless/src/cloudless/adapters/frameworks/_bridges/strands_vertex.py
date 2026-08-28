"""VertexStrandsModel — drive Strands against GCP Vertex AI Gemini natively.

Strands Agents ships `BedrockModel` only. This bridge subclasses
Strands' abstract `Model` and routes inference through `google.genai`
talking to Vertex AI — no LiteLLM, no third-party shim.

Usage:
    from strands import Agent
    from cloudless.adapters.frameworks._bridges import VertexStrandsModel

    @cloudless.agent(name="my_strands_on_gcp", framework="strands")
    class MyAgent(cloudless.StrandsAgent):
        def build(self):
            return Agent(
                name="my_strands_on_gcp",
                model=VertexStrandsModel(
                    model="gemini-2.0-flash",
                    project="my-gcp-project",
                    location="us-central1",
                ),
                system_prompt="Respond concisely.",
            )
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
from typing import Any, TypeVar

from strands.models.model import Model
from strands.types.content import Message
from strands.types.streaming import StreamEvent

T = TypeVar("T")


class VertexStrandsModel(Model):
    """Strands Model implementation that routes inference through Vertex AI.

    Translates Strands `Message[]` → google-genai `Content[]`, calls
    `google.genai.Client(vertexai=True).models.generate_content`, and
    maps the response back into Strands' `StreamEvent` shape
    (`messageStart` → `contentBlockDelta` → `contentBlockStop` →
    `messageStop`). This non-streaming initial implementation emits the
    full text as a single contentBlockDelta — sufficient for
    cloudless.StrandsAgent to emit `TextChunk` + `FinalChunk`.
    """

    def __init__(
        self,
        *,
        model: str = "gemini-2.0-flash",
        project: str | None = None,
        location: str = "us-central1",
        **kwargs: Any,
    ) -> None:
        self._model_id = model
        self._project = project
        self._location = location
        self._extra = kwargs
        # Lazy-init the google.genai client on first call so import time stays cheap.
        self._client: Any = None

    # ------------------------- Strands Model API ----------------------- #

    def get_config(self) -> dict[str, Any]:
        return {"model": self._model_id, "project": self._project, "location": self._location}

    def update_config(self, **model_config: Any) -> None:
        if "model" in model_config:
            self._model_id = model_config["model"]
        if "project" in model_config:
            self._project = model_config["project"]
        if "location" in model_config:
            self._location = model_config["location"]

    def _genai_client(self) -> Any:
        if self._client is None:
            from google import genai

            self._client = genai.Client(
                vertexai=True, project=self._project, location=self._location
            )
        return self._client

    async def stream(
        self,
        messages: list[Message],
        tool_specs: list[Any] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        client = self._genai_client()
        contents = self._strands_to_genai(messages)

        from google.genai import types as genai_types

        config = None
        if system_prompt:
            config = genai_types.GenerateContentConfig(system_instruction=system_prompt)

        # google-genai sync API — wrap to keep the event loop free.
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=self._model_id,
            contents=contents,
            config=config,
        )

        text = getattr(response, "text", "") or ""

        # Emit a minimal but well-formed Strands StreamEvent sequence.
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"delta": {"text": text}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}

        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            input_tokens = getattr(usage, "prompt_token_count", 0) or 0
            output_tokens = getattr(usage, "candidates_token_count", 0) or 0
            yield {
                "metadata": {
                    "usage": {
                        "inputTokens": input_tokens,
                        "outputTokens": output_tokens,
                        "totalTokens": input_tokens + output_tokens,
                    },
                    "metrics": {"latencyMs": 0},
                }
            }

    async def structured_output(
        self,
        output_model: type[T],
        prompt: list[Message],
        system_prompt: str | None = None,
        **kwargs: Any,
    ):  # type: ignore[override]
        # Structured-output route not implemented in this initial bridge.
        # Strands raises NotImplementedError here gracefully via the
        # parent class's documented contract.
        raise NotImplementedError(
            "VertexStrandsModel.structured_output is not implemented in v1; "
            "use the stream() path."
        )
        yield  # pragma: no cover — make this a generator for the abc

    # ----------------------- message translation ----------------------- #

    @staticmethod
    def _strands_to_genai(messages: list[Message]) -> list[Any]:
        """Strands Message[] → google.genai Content[]."""
        from google.genai import types as genai_types

        out: list[Any] = []
        for msg in messages:
            role = msg.get("role", "user")
            # google.genai expects "user" or "model"
            if role == "assistant":
                role = "model"
            content_blocks = msg.get("content", []) or []
            parts = []
            for block in content_blocks:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if text:
                    parts.append(genai_types.Part.from_text(text=text))
            if parts:
                out.append(genai_types.Content(role=role, parts=parts))
        return out
