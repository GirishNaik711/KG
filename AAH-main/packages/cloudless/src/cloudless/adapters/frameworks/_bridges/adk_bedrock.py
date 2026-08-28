"""BedrockADKLlm — drive Google ADK against AWS Bedrock natively.

Google ADK ships only `Gemini` and `LiteLlm` model classes out-of-the-box.
This bridge subclasses ADK's `BaseLlm` and routes inference through
`boto3.client('bedrock-runtime').converse` — no LiteLLM, no Anthropic
SDK shim. Users keep writing ADK agents (`google.adk.agents.Agent`)
exactly as they would for Vertex; cloudless swaps the model.

Usage:
    from google.adk.agents import Agent
    from cloudless.adapters.frameworks._bridges import BedrockADKLlm

    @cloudless.agent(name="my_adk_on_aws", framework="adk")
    class MyAgent(cloudless.ADKAgent):
        def build(self):
            return Agent(
                name="my_adk_on_aws",
                model=BedrockADKLlm(model="us.amazon.nova-micro-v1:0"),
                instruction="Respond concisely.",
            )
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai.types import Content, Part


class BedrockADKLlm(BaseLlm):
    """ADK BaseLlm implementation that routes inference through AWS Bedrock.

    Maps ADK `LlmRequest.contents` (google.genai Content/Part) to the
    Bedrock Converse message format, calls `converse`, and maps the
    response text back to an ADK `LlmResponse(content=Content(...))`.

    Streaming is not implemented in this initial bridge — ADK's
    streaming contract (`stream=True`) yields one `LlmResponse` per
    delta. We instead yield a single final response. ADK's downstream
    handlers tolerate this; the user-visible cloudless `Chunk` stream
    still produces one TextChunk + one FinalChunk.
    """

    model: str = "us.amazon.nova-micro-v1:0"
    """AWS Bedrock model id or inference-profile id (per F1 / F15)."""

    region: str = "us-east-1"
    """AWS region for the bedrock-runtime client."""

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        import boto3

        bedrock_messages, system_blocks = self._adk_to_bedrock(llm_request)
        client = boto3.client("bedrock-runtime", region_name=self.region)

        kwargs: dict[str, Any] = {
            "modelId": self.model,
            "messages": bedrock_messages,
        }
        if system_blocks:
            kwargs["system"] = system_blocks

        # boto3 is sync; wrap so the ADK runner's event loop isn't blocked.
        response = await asyncio.to_thread(client.converse, **kwargs)

        output = response.get("output", {})
        message = output.get("message", {})
        content_blocks = message.get("content", []) or []
        text = "".join(
            block.get("text", "") for block in content_blocks if isinstance(block, dict)
        )

        yield LlmResponse(
            content=Content(role="model", parts=[Part(text=text)]),
            turn_complete=True,
            partial=False,
            finish_reason="STOP",
        )

    @staticmethod
    def _adk_to_bedrock(
        request: LlmRequest,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Translate ADK contents → (Bedrock messages, Bedrock system blocks)."""
        bedrock_messages: list[dict[str, Any]] = []
        system_blocks: list[dict[str, Any]] = []

        # ADK's LlmRequest.config.system_instruction holds the instruction
        config = getattr(request, "config", None)
        system_text = ""
        if config is not None:
            si = getattr(config, "system_instruction", None)
            if isinstance(si, str):
                system_text = si
            elif si is not None:
                # google.genai Content with parts[0].text
                parts = getattr(si, "parts", None) or []
                system_text = "".join(getattr(p, "text", "") or "" for p in parts)
        if system_text:
            system_blocks.append({"text": system_text})

        for content in request.contents or []:
            role = getattr(content, "role", "user") or "user"
            # Bedrock Converse only supports "user" and "assistant" — map
            # "model" → "assistant".
            if role == "model":
                role = "assistant"
            if role not in ("user", "assistant"):
                continue
            parts = getattr(content, "parts", None) or []
            blocks: list[dict[str, Any]] = []
            for part in parts:
                text = getattr(part, "text", None)
                if text:
                    blocks.append({"text": text})
            if blocks:
                bedrock_messages.append({"role": role, "content": blocks})

        return bedrock_messages, system_blocks
