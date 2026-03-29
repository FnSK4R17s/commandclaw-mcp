"""OpenTelemetry OTLP tracing with auto-instrumentation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

if TYPE_CHECKING:
    from fastapi import FastAPI

    from commandclaw_mcp.config import ObservabilityConfig


def setup_tracing(config: ObservabilityConfig, app: FastAPI | None = None) -> TracerProvider:
    """Initialize OpenTelemetry tracing with OTLP export."""
    resource = Resource.create({"service.name": "commandclaw-mcp-gateway"})

    sampler = TraceIdRatioBased(config.traces_sampler_arg)
    provider = TracerProvider(resource=resource, sampler=sampler)

    exporter = OTLPSpanExporter(endpoint=config.otlp_endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)

    # Auto-instrument libraries
    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()

    if app is not None:
        FastAPIInstrumentor.instrument_app(app)

    return provider


def get_tracer(name: str = "commandclaw_mcp") -> trace.Tracer:
    """Get a tracer instance for manual span creation."""
    return trace.get_tracer(name)
