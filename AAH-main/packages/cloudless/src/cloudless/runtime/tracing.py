"""Q39 OpenTelemetry trace propagation.

Wraps `opentelemetry.trace` so cloudless calls emit spans for:
  - cloudless.LLM.invoke / .stream  (span name: "llm.invoke", "llm.stream")
  - cloudless.Tool.invoke           (span name: "tool.{name}")
  - A2APeerClient.call              (span name: "peer.{name}")
  - Agent.query (set by the embedded runtime)

OTel is an OPTIONAL dependency. If `opentelemetry-api` isn't installed,
all helpers degrade to no-ops so cloudless core has no hard dep.

Convention:
  - Service name: from CLOUDLESS_SERVICE_NAME env or "cloudless-agent"
  - Resource attributes injected: agent.name, agent.version, cloud
  - Span attributes follow gen-ai semconv where applicable:
      gen_ai.system = "bedrock" | "vertex"
      gen_ai.request.model = model_id
      gen_ai.usage.input_tokens, gen_ai.usage.output_tokens
"""
from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from typing import Any

_INITIALIZED = False
_TRACER = None  # opentelemetry.trace.Tracer or None


def _has_otel() -> bool:
    try:
        import opentelemetry.trace  # noqa: F401
        return True
    except ImportError:
        return False


def configure(
    *,
    service_name: str | None = None,
    agent_name: str | None = None,
    agent_version: str | None = None,
    cloud: str | None = None,
    exporter: str = "auto",
) -> None:
    """Configure OTel tracer. Idempotent. Safe to call without OTel installed.

    Args:
        service_name: OTel service.name resource attribute. Default
            CLOUDLESS_SERVICE_NAME env or "cloudless-agent".
        agent_name: Tagged as agent.name on every span.
        agent_version: Tagged as agent.version on every span.
        cloud: "aws" | "gcp". Tagged as cloud on every span.
        exporter: "auto" (use OTLP if OTEL_EXPORTER_OTLP_ENDPOINT is set,
            otherwise console), "otlp", "console", or "none".
    """
    global _INITIALIZED, _TRACER

    if _INITIALIZED:
        return
    if not _has_otel():
        _INITIALIZED = True
        return

    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
    )

    resource_attrs = {
        "service.name": service_name or os.environ.get("CLOUDLESS_SERVICE_NAME", "cloudless-agent"),
    }
    if agent_name:
        resource_attrs["agent.name"] = agent_name
    if agent_version:
        resource_attrs["agent.version"] = agent_version
    if cloud:
        resource_attrs["cloud"] = cloud

    provider = TracerProvider(resource=Resource.create(resource_attrs))

    # Pick exporter
    if exporter == "none":
        pass  # no processors → spans dropped
    elif exporter == "console":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    else:
        # auto / otlp
        otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )
                provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            except ImportError:
                # Fall back to console if OTLP exporter package missing
                provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        elif exporter == "otlp":
            # User requested OTLP but no endpoint: no-op
            pass
        elif exporter == "auto":
            # No endpoint + auto: stay silent (don't pollute stdout)
            pass

    # Don't override an existing provider — respect user setup.
    if isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
        trace.set_tracer_provider(provider)

    _TRACER = trace.get_tracer("cloudless")
    _INITIALIZED = True


def get_tracer() -> Any:
    """Return the OTel tracer (or None if OTel not installed)."""
    if not _INITIALIZED:
        configure()
    return _TRACER


# --------------------------------------------------------------------- #
# Span helpers — no-op when OTel isn't installed
# --------------------------------------------------------------------- #


@contextlib.contextmanager
def span(name: str, **attrs: Any) -> Iterator[Any]:
    """Open an OTel span with the given attributes; yields the span object
    (or None if OTel disabled).
    """
    tracer = get_tracer()
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(name) as sp:
        for k, v in attrs.items():
            if v is not None:
                try:
                    sp.set_attribute(k, v)
                except Exception:
                    pass
        yield sp


def set_attr(name: str, value: Any) -> None:
    """Set an attribute on the current span, if any."""
    if not _has_otel():
        return
    from opentelemetry import trace
    sp = trace.get_current_span()
    if sp is not None:
        try:
            sp.set_attribute(name, value)
        except Exception:
            pass


def record_exception(exc: BaseException) -> None:
    """Record an exception on the current span."""
    if not _has_otel():
        return
    from opentelemetry import trace
    sp = trace.get_current_span()
    if sp is not None:
        try:
            sp.record_exception(exc)
            sp.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
        except Exception:
            pass
