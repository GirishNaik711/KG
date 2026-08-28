"""Vertex Gemini LLM backend for cloudless.LLM.

Uses the new Google Gen AI SDK (`google-genai`) — replaces the legacy
`vertexai.generative_models` module which is being removed on 2026-06-24.

F2 mitigation: Gemini 2.5 includes extended thinking by default and the
`max_output_tokens` budget is shared between thinking and output. We
disable thinking by default (`thinking_config.thinking_budget=0`) so
small `max_output_tokens` values don't starve user-visible output. Users
who want thinking can pass `extended_thinking=True`.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from cloudless.chunks import TextChunk
from cloudless.exceptions import (
    AuthenticationError,
    InvalidInputError,
    ThrottledError,
)
from cloudless.exceptions import (
    TimeoutError as CloudlessTimeoutError,
)


class GeminiBackend:
    """google-genai-backed LLM. Real Gemini calls via Vertex AI."""

    def __init__(
        self,
        *,
        model_id: str,
        project: str,
        location: str = "us-central1",
        extended_thinking: bool = False,
        safety_settings: list[dict] | None = None,
        model_armor_template: str | None = None,
        grounding: bool | str = False,
        cached_content: str | None = None,
    ) -> None:
        """
        Args:
            safety_settings: List of {category, threshold} dicts per Gemini
                safety API. Categories: HARM_CATEGORY_HATE_SPEECH,
                HARM_CATEGORY_DANGEROUS_CONTENT,
                HARM_CATEGORY_SEXUALLY_EXPLICIT, HARM_CATEGORY_HARASSMENT.
                Thresholds: BLOCK_NONE, BLOCK_LOW_AND_ABOVE, BLOCK_MEDIUM_AND_ABOVE,
                BLOCK_ONLY_HIGH. Default: provider default.
            model_armor_template: Resource name of a GCP Model Armor template
                to apply on input and output. Cloud-native guardrails (parity
                with Bedrock Guardrails).
        """
        from google import genai
        self.model_id = model_id
        self.project = project
        self.location = location
        self.extended_thinking = extended_thinking
        self.safety_settings = safety_settings
        self.model_armor_template = model_armor_template
        self.grounding = grounding
        self.cached_content = cached_content
        self._client = genai.Client(vertexai=True, project=project, location=location)

    def create_cache(self, *, system_instruction: str, ttl_seconds: int = 3600) -> str:
        """Create a Gemini cachedContents object for a long system prompt.

        Returns the cache resource name (use it in subsequent invokes via
        the `cached_content` constructor kwarg). The cache survives across
        calls for `ttl_seconds`, billed at ~75% off vs. uncached input.

        Gemini requires the cached prompt to exceed a minimum token count
        (currently 1024 for Flash, 2048 for Pro).
        """
        cache = self._client.caches.create(
            model=self.model_id,
            config={
                "system_instruction": system_instruction,
                "ttl": f"{ttl_seconds}s",
            },
        )
        return cache.name

    # ------------------------------------------------------------------ #
    # Config builder — encodes F2 (Gemini 2.5 thinking budget)
    # ------------------------------------------------------------------ #

    def _build_config(
        self,
        *,
        system: str | None,
        max_tokens: int,
    ) -> dict:
        config: dict[str, Any] = {"max_output_tokens": max_tokens}
        if system:
            config["system_instruction"] = system
        if not self.extended_thinking and "gemini-2.5" in self.model_id:
            config["thinking_config"] = {"thinking_budget": 0}
        if self.safety_settings:
            config["safety_settings"] = self.safety_settings
        if self.model_armor_template:
            config["model_armor_config"] = {
                "prompt_template_name": self.model_armor_template,
                "response_template_name": self.model_armor_template,
            }
        if self.grounding:
            # bool True → Google Search; str → Vertex AI Search datastore resource
            if self.grounding is True:
                config["tools"] = [{"google_search": {}}]
            elif isinstance(self.grounding, str):
                # Custom datastore — Vertex Search corpus resource path:
                # projects/<num>/locations/<loc>/collections/.../dataStores/<id>
                config["tools"] = [{
                    "retrieval": {
                        "vertex_ai_search": {"datastore": self.grounding},
                    },
                }]
        if self.cached_content:
            config["cached_content"] = self.cached_content
        return config

    # ------------------------------------------------------------------ #
    # Sync request/response
    # ------------------------------------------------------------------ #

    async def invoke(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        ctx: Any = None,
        images: list[dict] | None = None,
        videos: list[dict] | None = None,
        audios: list[dict] | None = None,
    ) -> str:
        contents = self._build_contents(prompt, images, videos, audios)
        # google-genai is sync — off-load so asyncio.gather parallelizes.
        import asyncio
        try:
            resp = await asyncio.to_thread(
                self._client.models.generate_content,
                model=self.model_id,
                contents=contents,
                config=self._build_config(system=system, max_tokens=max_tokens),
            )
        except Exception as e:
            raise self._translate(e) from e

        text = getattr(resp, "text", None) or ""

        if ctx is not None and hasattr(ctx, "cost"):
            usage = getattr(resp, "usage_metadata", None)
            if usage:
                ctx.cost.record_llm_call(
                    model=self.model_id,
                    input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                    output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
                    cached_tokens=getattr(usage, "cached_content_token_count", 0) or 0,
                    reasoning_tokens=getattr(usage, "thoughts_token_count", 0) or 0,
                )
        return text

    # ------------------------------------------------------------------ #
    # Streaming
    # ------------------------------------------------------------------ #

    async def stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        ctx: Any = None,
        images: list[dict] | None = None,
        videos: list[dict] | None = None,
        audios: list[dict] | None = None,
    ) -> AsyncIterator[TextChunk]:
        contents = self._build_contents(prompt, images, videos, audios)
        import asyncio
        try:
            stream = await asyncio.to_thread(
                self._client.models.generate_content_stream,
                model=self.model_id,
                contents=contents,
                config=self._build_config(system=system, max_tokens=max_tokens),
            )
        except Exception as e:
            raise self._translate(e) from e

        in_tokens = 0
        out_tokens = 0
        reasoning_tokens = 0
        for chunk in stream:
            text = getattr(chunk, "text", None)
            if text:
                yield TextChunk(text=text)
            usage = getattr(chunk, "usage_metadata", None)
            if usage:
                in_tokens = getattr(usage, "prompt_token_count", 0) or in_tokens
                out_tokens = getattr(usage, "candidates_token_count", 0) or out_tokens
                reasoning_tokens = getattr(usage, "thoughts_token_count", 0) or reasoning_tokens

        if ctx is not None and hasattr(ctx, "cost"):
            ctx.cost.record_llm_call(
                model=self.model_id,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                reasoning_tokens=reasoning_tokens,
            )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _build_contents(
        self,
        prompt: str,
        images: list[dict] | None,
        videos: list[dict] | None = None,
        audios: list[dict] | None = None,
    ) -> Any:
        """Build the `contents` parameter for generate_content."""
        if not (images or videos or audios):
            return prompt
        from google.genai import types as gtypes
        parts: list[Any] = []
        for img in images or []:
            parts.append(gtypes.Part.from_bytes(
                data=img["data"],
                mime_type=img.get("mime_type", "image/jpeg"),
            ))
        for vid in videos or []:
            parts.append(gtypes.Part.from_bytes(
                data=vid["data"],
                mime_type=vid.get("mime_type", "video/mp4"),
            ))
        for aud in audios or []:
            parts.append(gtypes.Part.from_bytes(
                data=aud["data"],
                mime_type=aud.get("mime_type", "audio/mp3"),
            ))
        parts.append(prompt)
        return parts

    @staticmethod
    def _translate(e: Exception) -> Exception:
        # google-genai raises its own APIError hierarchy with a `.code` attribute
        # holding the HTTP status. Translate by status code.
        try:
            from google.genai import errors as genai_errors
        except ImportError:
            genai_errors = None  # type: ignore[assignment]

        if genai_errors is not None and isinstance(e, genai_errors.APIError):
            status = getattr(e, "code", None) or getattr(e, "status_code", None)
            msg = str(e)
            if status in (401, 403):
                return AuthenticationError(msg)
            if status == 429:
                return ThrottledError(msg)
            if status in (400, 404):
                return InvalidInputError(msg)
            if status == 504:
                return CloudlessTimeoutError(msg)
            # Other 4xx → invalid input (caller error). 5xx falls through as-is.
            if isinstance(status, int) and 400 <= status < 500:
                return InvalidInputError(msg)

        # google-api-core hierarchy (used by some Vertex paths)
        try:
            from google.api_core.exceptions import (
                DeadlineExceeded,
                InvalidArgument,
                NotFound,
                PermissionDenied,
                ResourceExhausted,
                Unauthenticated,
            )
        except ImportError:
            return e
        if isinstance(e, DeadlineExceeded):
            return CloudlessTimeoutError(str(e))
        if isinstance(e, ResourceExhausted):
            return ThrottledError(str(e))
        if isinstance(e, (Unauthenticated, PermissionDenied)):
            return AuthenticationError(str(e))
        if isinstance(e, (InvalidArgument, NotFound)):
            return InvalidInputError(str(e))
        return e
