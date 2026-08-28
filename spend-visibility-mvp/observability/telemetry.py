from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)
F = TypeVar("F", bound=Callable[..., Any])


def configure_dynatrace(app: Any) -> bool:
    """Attach FastAPI OpenTelemetry export to a Dynatrace OTLP endpoint."""
    endpoint = os.getenv("DYNATRACE_OTLP_ENDPOINT", "").strip()
    token = os.getenv("DYNATRACE_API_TOKEN", "").strip()
    if not endpoint or not token:
        logger.info("Dynatrace telemetry disabled: endpoint or API token is missing")
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", "spend-visibility-backend")})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces", headers={"Authorization": f"Api-Token {token}"})
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
        logger.info("Dynatrace OpenTelemetry export enabled")
        return True
    except ImportError:
        logger.warning("Dynatrace telemetry disabled: OpenTelemetry packages are not installed")
        return False


def trace_agent_run(function: F) -> F:
    """Decorate an agent call with a LangSmith trace when configured."""
    if not os.getenv("LANGSMITH_API_KEY", "").strip():
        return function
    try:
        from langsmith import traceable
    except ImportError:
        logger.warning("LangSmith telemetry disabled: langsmith is not installed")
        return function
    return traceable(name="spend-recommendation", run_type="chain")(function)  # type: ignore[return-value]