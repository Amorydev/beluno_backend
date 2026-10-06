"""Safe optional telemetry initialization for API and background processes."""

from __future__ import annotations

import logging
from typing import Any, cast

import sentry_sdk
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from sentry_sdk.types import Event, Hint

from beluno.config import Settings
from beluno.observability.redaction import redact


def configure_observability(app: FastAPI, settings: Settings) -> None:
    """Enable exporters only when explicitly configured; request bodies stay disabled."""

    if settings.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.sentry_dsn.get_secret_value(),
            environment=settings.environment.value,
            release=settings.release,
            before_send=redact_sentry_event,
            send_default_pii=False,
            # Request bodies carry sign-in secrets and personal data; never attach them.
            max_request_body_size="never",
        )
    if settings.otel_exporter_otlp_endpoint:
        resource = Resource.create(
            {
                SERVICE_NAME: "beluno-api",
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
        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls="health/live,health/ready",
        )


def redact_sentry_event(event: Event, _: Hint) -> Event | None:
    """Sentry's final boundary: no payload reaches the exporter unredacted."""

    return cast(Event, redact(event))


def safe_extra(**fields: Any) -> dict[str, Any]:
    """Return a redacted logging ``extra`` payload for structured loggers."""

    return {"extra": redact(fields)}


def logger(name: str) -> logging.Logger:
    """Return a named logger; callers pass fields through ``safe_extra``."""

    return logging.getLogger(name)
