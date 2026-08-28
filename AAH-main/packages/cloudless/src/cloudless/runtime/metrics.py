"""Cloud-native metric emission.

CloudWatch (AWS) and Cloud Monitoring (GCP) both accept structured custom
metrics. cloudless emits per-invocation metrics for:

  - llm.invoke_latency_ms          (per LLM call)
  - llm.input_tokens, output_tokens
  - tool.invoke_latency_ms         (per tool call)
  - peer.call_latency_ms           (per A2A peer call)
  - session.total_usd              (at end of session)

Default: no emission (zero overhead). Users opt-in by calling
`configure_cloudwatch(namespace=..., region=...)` or
`configure_cloud_monitoring(project=...)` at process start.

The emitters are lazy: SDK imports happen only when configure_* is called.
"""
from __future__ import annotations

import os
import time
from typing import Any

# --------------------------------------------------------------------- #
# CloudWatch emitter (AWS)
# --------------------------------------------------------------------- #


_CW_CLIENT: Any = None
_CW_NAMESPACE: str = "cloudless"


def configure_cloudwatch(*, namespace: str = "cloudless", region: str = "us-east-1") -> None:
    """Enable CloudWatch metric emission. Call once at process start."""
    global _CW_CLIENT, _CW_NAMESPACE
    import boto3
    _CW_CLIENT = boto3.client("cloudwatch", region_name=region)
    _CW_NAMESPACE = namespace


def emit_cloudwatch_metric(
    *,
    name: str,
    value: float,
    unit: str = "None",
    dimensions: dict[str, str] | None = None,
) -> None:
    """Emit one custom metric to CloudWatch. No-op if not configured.

    Args:
        name: Metric name, e.g. "LLMInvokeLatencyMs"
        value: Metric value (numeric).
        unit: CloudWatch unit string ("Milliseconds", "Count", "None", etc.).
        dimensions: Optional dict of {dimension_name: value}.
    """
    if _CW_CLIENT is None:
        return
    metric_dim = []
    if dimensions:
        metric_dim = [{"Name": k, "Value": v} for k, v in dimensions.items()]
    try:
        _CW_CLIENT.put_metric_data(
            Namespace=_CW_NAMESPACE,
            MetricData=[{
                "MetricName": name,
                "Value": float(value),
                "Unit": unit,
                "Dimensions": metric_dim,
            }],
        )
    except Exception:
        pass  # best-effort emission


# --------------------------------------------------------------------- #
# Cloud Monitoring emitter (GCP)
# --------------------------------------------------------------------- #


_CM_CLIENT: Any = None
_CM_PROJECT: str | None = None


def configure_cloud_monitoring(*, project: str) -> None:
    """Enable Cloud Monitoring metric emission. Call once at process start."""
    global _CM_CLIENT, _CM_PROJECT
    try:
        from google.cloud import monitoring_v3
    except ImportError as e:
        raise RuntimeError("google-cloud-monitoring is required") from e
    _CM_CLIENT = monitoring_v3.MetricServiceClient()
    _CM_PROJECT = project


def emit_cloud_monitoring_metric(
    *,
    name: str,
    value: float,
    labels: dict[str, str] | None = None,
) -> None:
    """Emit one custom metric to Cloud Monitoring. No-op if not configured."""
    if _CM_CLIENT is None or _CM_PROJECT is None:
        return
    try:
        from google.cloud import monitoring_v3

        series = monitoring_v3.TimeSeries()
        series.metric.type = f"custom.googleapis.com/cloudless/{name}"
        if labels:
            for k, v in labels.items():
                series.metric.labels[k] = v
        series.resource.type = "global"
        # Set the value as a double
        now = time.time()
        seconds = int(now)
        nanos = int((now - seconds) * 10**9)
        interval = monitoring_v3.TimeInterval({"end_time": {"seconds": seconds, "nanos": nanos}})
        point = monitoring_v3.Point({
            "interval": interval,
            "value": {"double_value": float(value)},
        })
        series.points = [point]
        _CM_CLIENT.create_time_series(
            name=f"projects/{_CM_PROJECT}",
            time_series=[series],
        )
    except Exception:
        pass  # best-effort


# --------------------------------------------------------------------- #
# Unified emit (auto-detects which emitters are configured)
# --------------------------------------------------------------------- #


def emit_metric(
    *,
    name: str,
    value: float,
    unit: str = "None",
    dimensions: dict[str, str] | None = None,
) -> None:
    """Emit to every configured emitter."""
    emit_cloudwatch_metric(name=name, value=value, unit=unit, dimensions=dimensions)
    emit_cloud_monitoring_metric(name=name, value=value, labels=dimensions)


# --------------------------------------------------------------------- #
# X-Ray + Cloud Trace OTel exporters
# --------------------------------------------------------------------- #


def configure_xray_export(*, sampler_ratio: float = 0.1) -> None:
    """Configure OTel to export spans to AWS X-Ray. No-op if SDK missing."""
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
    except ImportError:
        return  # no-op

    # AWS Distro for OpenTelemetry uses an OTLP collector running as a
    # sidecar that forwards to X-Ray. Endpoint defaults to localhost:4318.
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318/v1/traces")
    provider = TracerProvider(sampler=TraceIdRatioBased(sampler_ratio))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    if isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
        trace.set_tracer_provider(provider)


def configure_cloud_trace_export(*, project: str) -> None:
    """Configure OTel to export spans to GCP Cloud Trace.

    Requires `opentelemetry-exporter-gcp-trace` (optional dep).
    """
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        return  # no-op

    provider = TracerProvider()
    provider.add_span_processor(
        BatchSpanProcessor(CloudTraceSpanExporter(project_id=project))  # type: ignore[no-untyped-call]
    )
    if isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
        trace.set_tracer_provider(provider)
