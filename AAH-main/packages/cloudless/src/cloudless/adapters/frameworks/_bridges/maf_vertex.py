"""VertexMAFChatClient — drive Microsoft Agent Framework against Vertex AI natively.

`agent-framework-bedrock` ships `BedrockChatClient` for AWS, but there
is no official `agent-framework-vertex` plug-in. This bridge subclasses
MAF's `BaseChatClient` and routes inference through `google.genai`
against Vertex AI — no LiteLLM, no third-party shim.

Usage:
    from agent_framework import Agent
    from cloudless.adapters.frameworks._bridges import VertexMAFChatClient

    @cloudless.agent(name="my_maf_on_gcp", framework="maf")
    class MyAgent(cloudless.MAFAgent):
        def build(self):
            return Agent(
                VertexMAFChatClient(
                    model="gemini-2.0-flash",
                    project="my-gcp-project",
                    location="us-central1",
                ),
                instructions="Respond concisely.",
                name="my_maf_on_gcp",
            )
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, Mapping, Sequence
from typing import Any

from agent_framework import (
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
)


class VertexMAFChatClient(BaseChatClient):
    """MAF BaseChatClient implementation routing inference through Vertex AI.

    Translates MAF `Sequence[Message]` → google-genai `Content[]`,
    calls `generate_content` non-streamed, returns one MAF `ChatResponse`.
    """

    OTEL_PROVIDER_NAME: str = "google.vertex_ai"

    def __init__(
        self,
        *,
        model: str = "gemini-2.0-flash",
        project: str | None = None,
        location: str = "us-central1",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._model_id = model
        self._project = project
        self._location = location
        self._client: Any = None

    @property
    def service_url(self) -> str | None:
        # MAF uses this for OTel span attribution.
        return (
            f"https://{self._location}-aiplatform.googleapis.com/"
            f"projects/{self._project}/locations/{self._location}"
        )

    def _genai_client(self) -> Any:
        if self._client is None:
            from google import genai

            self._client = genai.Client(
                vertexai=True, project=self._project, location=self._location
            )
        return self._client

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any:
        """Return either a coroutine (stream=False) or a ResponseStream (stream=True).

        Mirrors the pattern in `agent_framework_bedrock.BedrockChatClient`:
        the method itself is sync; the streaming case wraps an async
        generator in `self._build_response_stream(...)`.
        """
        if stream:
            async def _stream() -> AsyncIterable[ChatResponseUpdate]:
                text = await self._invoke(messages)
                yield ChatResponseUpdate(
                    contents=[Content.from_text(text)],
                    role="assistant",
                    finish_reason="stop",
                )

            return self._build_response_stream(_stream())

        async def _get_response() -> ChatResponse:
            text = await self._invoke(messages)
            return ChatResponse(
                messages=[
                    Message(role="assistant", contents=[Content.from_text(text)])
                ]
            )

        return _get_response()

    async def _invoke(self, messages: Sequence[Message]) -> str:
        """Single Vertex inference call, returning the assistant text."""
        client = self._genai_client()
        contents, system_instruction = self._maf_to_genai(messages)

        from google.genai import types as genai_types

        config = None
        if system_instruction:
            config = genai_types.GenerateContentConfig(
                system_instruction=system_instruction
            )

        response = await asyncio.to_thread(
            client.models.generate_content,
            model=self._model_id,
            contents=contents,
            config=config,
        )
        return getattr(response, "text", "") or ""

    @staticmethod
    def _maf_to_genai(
        messages: Sequence[Message],
    ) -> tuple[list[Any], str | None]:
        """MAF Message[] → (google-genai Content[], system_instruction string)."""
        from google.genai import types as genai_types

        contents: list[Any] = []
        system_text_parts: list[str] = []

        for msg in messages:
            role = msg.role or "user"
            text_parts = []
            for content in msg.contents or []:
                ctype = getattr(content, "type", None)
                if ctype == "text":
                    t = getattr(content, "text", "") or ""
                    if t:
                        text_parts.append(t)
                # Other content types (function_call/result, images) are
                # left to a follow-up enhancement — this initial bridge
                # handles text-in/text-out.
            if not text_parts:
                continue

            if role == "system":
                system_text_parts.extend(text_parts)
                continue

            # MAF role → google-genai role
            genai_role = "model" if role == "assistant" else "user"
            contents.append(
                genai_types.Content(
                    role=genai_role,
                    parts=[genai_types.Part.from_text(text=t) for t in text_parts],
                )
            )

        return contents, "\n".join(system_text_parts) or None
