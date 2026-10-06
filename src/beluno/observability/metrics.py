"""Reliability metrics: commands, sync traffic, change volume, and job queue health.

Instruments come from the OpenTelemetry meter provider configured at startup;
without an exporter they are no-ops. Attribute values are low-cardinality
names and outcomes only, never identifiers or payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Counter, Histogram, Meter, Observation

_provider: metrics.MeterProvider | None = None
_instruments: Instruments | None = None
_queue_health: dict[str, float] = {}


@dataclass(frozen=True)
class Instruments:
    commands: Counter
    command_duration: Histogram
    command_retries: Counter
    push_results: Counter
    pull_pages: Counter
    pull_items: Counter
    changes_appended: Counter
    changes_compacted: Counter
    operations_purged: Counter


def _observe_queue(options: CallbackOptions) -> list[Observation]:
    del options
    return [Observation(value, {"measure": name}) for name, value in _queue_health.items()]


def _build(meter: Meter) -> Instruments:
    meter.create_observable_gauge(
        "beluno.jobs.queue",
        callbacks=[_observe_queue],
        description="Job queue health: oldest waiting age in seconds and status depths",
    )
    return Instruments(
        commands=meter.create_counter(
            "beluno.commands", description="Command executions by name, source, and outcome"
        ),
        command_duration=meter.create_histogram(
            "beluno.command.duration", unit="ms", description="Command latency by name"
        ),
        command_retries=meter.create_counter(
            "beluno.command.retries", description="Transactions retried after 40001/40P01"
        ),
        push_results=meter.create_counter(
            "beluno.sync.push.results", description="Push operation results by outcome"
        ),
        pull_pages=meter.create_counter(
            "beluno.sync.pull.pages", description="Pull pages by scope type and status"
        ),
        pull_items=meter.create_counter(
            "beluno.sync.pull.items", description="Change items delivered by scope type"
        ),
        changes_appended=meter.create_counter(
            "beluno.sync.changes.appended", description="Change-log rows written"
        ),
        changes_compacted=meter.create_counter(
            "beluno.sync.changes.compacted", description="Change-log rows removed by retention"
        ),
        operations_purged=meter.create_counter(
            "beluno.sync.operations.purged", description="Expired operation records removed"
        ),
    )


def use_meter_provider(provider: metrics.MeterProvider | None) -> None:
    """Bind instruments to ``provider`` (``None`` falls back to the global one)."""

    global _provider, _instruments
    _provider = provider
    _instruments = None


def instruments() -> Instruments:
    global _instruments
    if _instruments is None:
        provider = _provider or metrics.get_meter_provider()
        _instruments = _build(provider.get_meter("beluno"))
    return _instruments


def report_queue_health(values: dict[str, float]) -> None:
    """Cache the latest queue measurements for the observable gauge."""

    _queue_health.clear()
    _queue_health.update(values)


def attributes(**values: Any) -> dict[str, str]:
    return {key: str(value) for key, value in values.items() if value is not None}
