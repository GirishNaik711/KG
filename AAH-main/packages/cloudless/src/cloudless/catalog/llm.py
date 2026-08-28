"""cloudless.LLM — unified LLM primitive over Bedrock / Gemini.

Design intent (per Q9 service catalog + F1/F2/F15 findings):

  - User writes `llm = cloudless.LLM(model="nova-micro")` with a logical
    name. The primitive resolves to the right cloud + concrete model ID
    (inference profile on Bedrock — F1) at construction.

  - Streaming via `async for chunk in llm.stream(prompt)` yields
    `cloudless.TextChunk` instances so the output composes with framework
    adapters and the Chunk taxonomy (Q16).

  - The cost tracker on `cloudless.Context` records each call for cost
    attribution (Q20).

  - F15 default: `nova-micro` is the safest streaming-compatible model
    in M1. Anthropic models require a use-case form for converse_stream
    in addition to converse — we surface a deploy-time warning.

M1 ships Bedrock backend only. M2 adds Gemini via google-genai.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
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

# --------------------------------------------------------------------- #
# Model alias table — the OQ4 "model alias resolution table" implementation.
# Keep in sync with what Bedrock + Gemini Model Garden actually offer.
# Note: ALL Bedrock entries use inference-profile IDs (us. prefix) per F1.
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelAlias:
    """One row of the alias table."""

    alias: str
    """Logical name users write: 'claude-haiku', 'nova-micro', etc."""

    provider: str
    """One of {'bedrock', 'gemini'}."""

    model_id: str
    """Concrete model identifier (Bedrock inference profile or Gemini model)."""

    streaming_safe: bool = True
    """False if streaming requires extra opt-in (e.g., Anthropic streaming form — F15)."""

    notes: str = ""


# Default alias table. cloudless v0.x bakes this in; v1+ can override
# from a registry or refresh on `cloudless upgrade-check`.
DEFAULT_ALIASES: tuple[ModelAlias, ...] = (
    # Amazon Nova family — F15-safe for streaming, no Anthropic form gating
    ModelAlias("nova-micro", "bedrock", "us.amazon.nova-micro-v1:0",
               notes="Default for M1 — cheap, fast, streaming-safe."),
    ModelAlias("nova-lite", "bedrock", "us.amazon.nova-lite-v1:0"),
    ModelAlias("nova-pro", "bedrock", "us.amazon.nova-pro-v1:0"),
    ModelAlias("nova-premier", "bedrock", "us.amazon.nova-premier-v1:0"),
    # Anthropic Claude on Bedrock — flagged streaming_safe=False per F15
    ModelAlias("claude-haiku", "bedrock", "us.anthropic.claude-haiku-4-5-20251001-v1:0",
               streaming_safe=False,
               notes="Requires Anthropic use-case form for converse_stream — F15."),
    ModelAlias("claude-sonnet", "bedrock", "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
               streaming_safe=False),
    ModelAlias("claude-opus", "bedrock", "us.anthropic.claude-opus-4-7",
               streaming_safe=False,
               notes="Use-case form required AND opus-tier review per F6."),
    # Gemini — M2 brings the Gemini adapter; aliases registered for forward-compat
    ModelAlias("gemini-flash", "gemini", "gemini-2.5-flash",
               notes="Gemini 2.5 extended thinking on by default — F2."),
    ModelAlias("gemini-pro", "gemini", "gemini-2.5-pro"),
)

_BY_ALIAS = {a.alias: a for a in DEFAULT_ALIASES}
_BY_MODEL_ID = {a.model_id: a for a in DEFAULT_ALIASES}


def resolve_model(name: str) -> ModelAlias:
    """Resolve a logical alias OR raw model ID to a `ModelAlias`.

    Accepts:
      - Alias (e.g. 'nova-micro') → lookup in DEFAULT_ALIASES
      - Concrete model ID (e.g. 'us.amazon.nova-micro-v1:0') → reverse lookup
      - Unknown raw model ID → wraps in a Bedrock alias with no metadata

    Raises:
        InvalidInputError if `name` looks like neither.
    """
    if name in _BY_ALIAS:
        return _BY_ALIAS[name]
    if name in _BY_MODEL_ID:
        return _BY_MODEL_ID[name]
    # Plausibly a raw Bedrock model ID we don't know about — pass through
    if name.startswith(("us.", "anthropic.", "amazon.", "meta.", "ai21.", "cohere.", "mistral.")):
        return ModelAlias(alias=name, provider="bedrock", model_id=name,
                          streaming_safe=False,  # unknown — pessimistic
                          notes="Unknown model ID — pass-through. Streaming safety unverified.")
    raise InvalidInputError(
        f"Unknown LLM model: {name!r}. Either use a registered alias "
        f"({', '.join(sorted(_BY_ALIAS))}) or a fully-qualified provider model ID."
    )


# --------------------------------------------------------------------- #
# The LLM primitive
# --------------------------------------------------------------------- #


class LLM:
    """Unified LLM client over Bedrock (M1) / Gemini (M2).

    Example:
        llm = cloudless.LLM(model="nova-micro")
        text = await llm.invoke("hello", system="be brief")

        async for chunk in llm.stream("hello"):
            print(chunk.text, end="")
    """

    def __init__(
        self,
        model: str = "nova-micro",
        *,
        region: str = "us-east-1",
        client: Any = None,
        project: str | None = None,
        location: str | None = None,
        extended_thinking: bool = False,
        guardrail_id: str | None = None,
        guardrail_version: str | None = "DRAFT",
        safety_settings: list[dict] | None = None,
        model_armor_template: str | None = None,
        grounding: bool | str = False,
        cached_content: str | None = None,
    ) -> None:
        """
        Args:
            model: Logical alias or raw model ID. See DEFAULT_ALIASES.
            region: AWS region for Bedrock. Default us-east-1.
            client: Optional pre-built boto3 client (for testing Bedrock path).
            project: GCP project for Gemini. Defaults to env CLOUDLESS_GCP_PROJECT
                or google.auth.default() project.
            location: GCP location for Gemini. Default us-central1.
            extended_thinking: If True, allow Gemini 2.5 to allocate thinking
                budget from `max_tokens` (F2). Default False.
        """
        self.alias = resolve_model(model)
        self.region = region
        self.guardrail_id = guardrail_id
        self.guardrail_version = guardrail_version
        self._backend: Any = None  # GeminiBackend instance for provider="gemini"

        if self.alias.provider == "bedrock":
            # Defer the boto3 import so cloudless can be imported without aws extras.
            import boto3
            self._client = client or boto3.client("bedrock-runtime", region_name=region)
        elif self.alias.provider == "gemini":
            from cloudless.adapters.gcp.llm import GeminiBackend
            resolved_project = project or self._resolve_gcp_project()
            resolved_location = location or "us-central1"
            self._backend = GeminiBackend(
                model_id=self.alias.model_id,
                project=resolved_project,
                location=resolved_location,
                extended_thinking=extended_thinking,
                safety_settings=safety_settings,
                model_armor_template=model_armor_template,
                grounding=grounding,
                cached_content=cached_content,
            )
        else:
            raise InvalidInputError(f"Unknown provider {self.alias.provider!r}")

    @staticmethod
    def _resolve_gcp_project() -> str:
        """Resolve GCP project from env or ADC."""
        import os
        env_project = (
            os.environ.get("CLOUDLESS_GCP_PROJECT")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("GCLOUD_PROJECT")
        )
        if env_project:
            return env_project
        try:
            import google.auth
            _, project = google.auth.default()
            if project:
                return project
        except Exception:
            pass
        raise InvalidInputError(
            "GCP project required for Gemini. Set CLOUDLESS_GCP_PROJECT or pass project=..."
        )

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
        """Single-shot request/response. Returns the assistant's text reply.

        Args:
            prompt: User input.
            system: Optional system prompt.
            max_tokens: Cap on output tokens. Default 512 — generous enough
                that Gemini 2.5's thinking budget doesn't starve output (F2).
            ctx: Optional `cloudless.Context` for cost-attribution recording.
            images: Optional list of {data: bytes, mime_type: str} dicts. Each
                image is sent alongside the text prompt. Bedrock supports this
                on Nova Lite/Pro and Claude; Gemini supports it on every model.
            videos: Optional list of {data: bytes, mime_type: str} dicts. Gemini
                supports inline video; Bedrock Nova Lite/Pro support video.
            audios: Optional list of {data: bytes, mime_type: str} dicts. Gemini
                supports inline audio (voice notes, music samples).

        Raises:
            AuthenticationError: Bedrock returned 401/403.
            ThrottledError: Bedrock returned 429.
            CloudlessTimeoutError: Boto3 read timeout.
            InvalidInputError: Bedrock returned 400.
        """
        from cloudless.runtime import tracing
        from cloudless.runtime.policy import get_registry
        reg = get_registry()
        prompt = reg.run("before_llm", prompt=prompt, model=self.alias.model_id, ctx=ctx)["prompt"]

        with tracing.span(
            "llm.invoke",
            **{
                "gen_ai.system": "bedrock" if self.alias.provider == "bedrock" else "vertex",
                "gen_ai.request.model": self.alias.model_id,
                "gen_ai.request.max_tokens": max_tokens,
            },
        ):
            try:
                if self._backend is not None:
                    text = await self._backend.invoke(
                        prompt, system=system, max_tokens=max_tokens, ctx=ctx,
                        images=images, videos=videos, audios=audios,
                    )
                    return reg.run("after_llm", prompt=prompt, response=text,
                                   model=self.alias.model_id, ctx=ctx)["response"]

                content: list[dict[str, Any]] = [{"text": prompt}]
                if images:
                    for img in images:
                        content.append({
                            "image": {
                                "format": _bedrock_image_format(img.get("mime_type", "")),
                                "source": {"bytes": img["data"]},
                            },
                        })
                if videos:
                    for vid in videos:
                        content.append({
                            "video": {
                                "format": _bedrock_video_format(vid.get("mime_type", "")),
                                "source": {"bytes": vid["data"]},
                            },
                        })
                # Bedrock Converse does not accept inline audio at v1; users
                # who need audio should call Bedrock Audio APIs directly.
                if audios and self.alias.provider == "bedrock":
                    raise InvalidInputError(
                        "Bedrock Converse does not accept inline audio input. "
                        "Use a Vertex Gemini model for audio."
                    )
                messages = [{"role": "user", "content": content}]
                kwargs: dict[str, Any] = {
                    "modelId": self.alias.model_id,
                    "messages": messages,
                    "inferenceConfig": {"maxTokens": max_tokens},
                }
                if system:
                    kwargs["system"] = [{"text": system}]
                if self.guardrail_id:
                    kwargs["guardrailConfig"] = {
                        "guardrailIdentifier": self.guardrail_id,
                        "guardrailVersion": self.guardrail_version or "DRAFT",
                        "trace": "enabled",
                    }

                # boto3 is sync; off-load to a thread so asyncio.gather can
                # actually parallelize concurrent invokes across calls.
                import asyncio
                try:
                    resp = await asyncio.to_thread(self._client.converse, **kwargs)
                except Exception as e:
                    raise self._translate_exception(e) from e

                stop_reason = resp.get("stopReason", "")
                if stop_reason == "guardrail_intervened":
                    from cloudless.exceptions import GuardrailBlocked
                    from cloudless.runtime.audit import emit_audit
                    trace = resp.get("trace", {}).get("guardrail", {})
                    emit_audit(
                        stage="after_llm",
                        decision="block",
                        policy_name=f"bedrock-guardrail/{self.guardrail_id}",
                        reason="Bedrock Guardrail intervened",
                        payload=prompt,
                        extra={"guardrail_trace": trace},
                    )
                    raise GuardrailBlocked(
                        f"Bedrock Guardrail {self.guardrail_id} blocked the response"
                    )

                text_parts: list[str] = []
                for block in resp["output"]["message"]["content"]:
                    if "text" in block:
                        text_parts.append(block["text"])
                result = "".join(text_parts)

                if ctx is not None and hasattr(ctx, "cost"):
                    usage = resp.get("usage", {})
                    in_tok = usage.get("inputTokens", 0)
                    out_tok = usage.get("outputTokens", 0)
                    ctx.cost.record_llm_call(
                        model=self.alias.model_id,
                        input_tokens=in_tok,
                        output_tokens=out_tok,
                        cached_tokens=usage.get("cacheReadInputTokens", 0),
                        reasoning_tokens=0,
                    )
                    tracing.set_attr("gen_ai.usage.input_tokens", in_tok)
                    tracing.set_attr("gen_ai.usage.output_tokens", out_tok)

                return reg.run("after_llm", prompt=prompt, response=result,
                               model=self.alias.model_id, ctx=ctx)["response"]
            except Exception as e:
                tracing.record_exception(e)
                raise

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
    ) -> AsyncIterator[TextChunk]:
        """Streaming variant — yields cloudless TextChunks as tokens arrive."""
        from cloudless.runtime.policy import get_registry
        reg = get_registry()
        prompt = reg.run("before_llm", prompt=prompt, model=self.alias.model_id, ctx=ctx)["prompt"]

        if self._backend is not None:
            async for chunk in self._backend.stream(
                prompt, system=system, max_tokens=max_tokens, ctx=ctx, images=images,
            ):
                yield chunk
            return

        if not self.alias.streaming_safe:
            # Warn but proceed — the form may have been approved for this account.
            # If it hasn't, Bedrock will raise ResourceNotFoundException with the
            # Anthropic form message and the caller gets InvalidInputError.
            import warnings
            warnings.warn(
                f"Model {self.alias.alias!r} ({self.alias.model_id}) is flagged "
                f"streaming_safe=False (F15). If converse_stream fails, fill out "
                f"Anthropic's use-case form or switch to a Nova alias.",
                stacklevel=2,
            )

        messages = [{"role": "user", "content": [{"text": prompt}]}]
        kwargs: dict[str, Any] = {
            "modelId": self.alias.model_id,
            "messages": messages,
            "inferenceConfig": {"maxTokens": max_tokens},
        }
        if system:
            kwargs["system"] = [{"text": system}]

        import asyncio
        try:
            resp = await asyncio.to_thread(self._client.converse_stream, **kwargs)
        except Exception as e:
            raise self._translate_exception(e) from e

        input_tokens = 0
        output_tokens = 0

        for event in resp["stream"]:
            if "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                if "text" in delta:
                    yield TextChunk(text=delta["text"])
            elif "metadata" in event:
                usage = event["metadata"].get("usage", {})
                input_tokens = usage.get("inputTokens", 0)
                output_tokens = usage.get("outputTokens", 0)
            elif "messageStop" in event:
                # End of stream — keep iterating to drain any trailing metadata events.
                pass

        if ctx is not None and hasattr(ctx, "cost"):
            ctx.cost.record_llm_call(
                model=self.alias.model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _translate_exception(self, e: Exception) -> Exception:
        """Map boto3 exceptions to cloudless exception hierarchy (Q21)."""
        try:
            from botocore.exceptions import ClientError, ReadTimeoutError
        except ImportError:
            return e

        if isinstance(e, ReadTimeoutError):
            return CloudlessTimeoutError(str(e))

        if isinstance(e, ClientError):
            code = e.response.get("Error", {}).get("Code", "")
            msg = str(e)
            if code in ("ThrottlingException", "ServiceQuotaExceededException", "TooManyRequestsException"):
                return ThrottledError(msg)
            if code in ("UnauthorizedException", "AccessDeniedException"):
                return AuthenticationError(msg)
            if code in ("ValidationException", "ResourceNotFoundException"):
                return InvalidInputError(msg)
        return e


def list_models(provider: str | None = None) -> Iterable[ModelAlias]:
    """Yield available model aliases, optionally filtered by provider."""
    for a in DEFAULT_ALIASES:
        if provider is None or a.provider == provider:
            yield a


def _bedrock_image_format(mime_type: str) -> str:
    """Map an image MIME type to the Bedrock `converse` format string."""
    mt = (mime_type or "").lower()
    if "png" in mt:
        return "png"
    if "jpeg" in mt or "jpg" in mt:
        return "jpeg"
    if "gif" in mt:
        return "gif"
    if "webp" in mt:
        return "webp"
    return "jpeg"


def _bedrock_video_format(mime_type: str) -> str:
    """Map a video MIME type to the Bedrock `converse` video format string."""
    mt = (mime_type or "").lower()
    if "mp4" in mt:
        return "mp4"
    if "webm" in mt:
        return "webm"
    if "mov" in mt or "quicktime" in mt:
        return "mov"
    if "mkv" in mt or "matroska" in mt:
        return "mkv"
    if "flv" in mt:
        return "flv"
    if "wmv" in mt:
        return "wmv"
    if "mpeg" in mt or "mpg" in mt:
        return "mpeg"
    if "three_gpp" in mt or "3gpp" in mt:
        return "three_gp"
    return "mp4"  # safe default
