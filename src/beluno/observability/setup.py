"""Safe optional telemetry initialization for API and background processes."""

from __future__ import annotations

import logging
from typing import Any, cast

import sentry_sdk
from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from sentry_sdk.types import Event, Hint

from beluno.config import Settings
from beluno.observability import metrics as reliability_metrics
from beluno.observability.redaction import redact


def configure_observability(app: FastAPI | None, settings: Settings) -> None:
    """Enable exporters only when explicitly configured; request bodies stay disabled."""

    if settings.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.sentry_dsn.get_secret_value(),
            environment=settings.environment.value,
            release=settings.release,
            before_send=redact_sentry_event,
            send_default_pii=False,
            # Frame locals hold request payloads (booking codes, notes): never send them.
            include_local_variables=False,
            # Request bodies carry sign-in secrets and personal data; never attach them.
            max_request_body_size="never",
        )
    if settings.otel_exporter_otlp_endpoint:
        resource = Resource.create(
            {
                SERVICE_NAME: "beluno-api" if app is not None else "beluno-worker",
                SERVICE_VERSION: settings.release,
                "deployment.environment.name": settings.environment.value,
            }
        )
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint),
            )
        )
        trace.set_tracer_provider(provider)
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=settings.otel_exporter_otlp_endpoint)
        )
        meter_provider = MeterProvider(
            resource=resource, metric_readers=[reader], views=latency_views()
        )
        metrics.set_meter_provider(meter_provider)
        reliability_metrics.use_meter_provider(meter_provider)
        if app is not None:
            instrument_api(app)


# Millisecond bucket bounds that include every latency target (300 ms, 1 s), so
# alerts on p95 compare against a real boundary instead of interpolating.
LATENCY_BOUNDS_MS = (5, 10, 25, 50, 100, 200, 300, 500, 750, 1_000, 2_000, 5_000, 10_000)


def latency_views() -> list[View]:
    aggregation = ExplicitBucketHistogramAggregation(boundaries=LATENCY_BOUNDS_MS)
    return [
        View(instrument_name=name, aggregation=aggregation)
        for name in (
            "http.server.duration",
            "beluno.command.duration",
            "beluno.finance.ledger_lock.wait",
        )
    ]


def instrument_api(app: FastAPI, tracer_provider: TracerProvider | None = None) -> None:
    """Trace HTTP requests by route and status only: no headers or bodies are captured."""

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider,
        excluded_urls="health/live,health/ready",
    )


def redact_sentry_event(event: Event, _: Hint) -> Event | None:
    """Sentry's final boundary: no payload reaches the exporter unredacted.

    Stack frames lose their local variables too, whatever the SDK setting.
    """

    for exception in (event.get("exception") or {}).get("values") or []:
        for frame in (exception.get("stacktrace") or {}).get("frames") or []:
            frame.pop("vars", None)
    for thread in (event.get("threads") or {}).get("values") or []:
        for frame in (thread.get("stacktrace") or {}).get("frames") or []:
            frame.pop("vars", None)
    return cast(Event, redact(event))


def safe_extra(**fields: Any) -> dict[str, Any]:
    """Return a redacted logging ``extra`` payload for structured loggers."""

    return {"extra": redact(fields)}


def logger(name: str) -> logging.Logger:
    """Return a named logger; callers pass fields through ``safe_extra``."""

    return logging.getLogger(name)
