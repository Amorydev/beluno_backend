"""Reliability instruments record to whichever meter provider is bound."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from beluno.observability import metrics


@pytest.fixture
def reader() -> Iterator[InMemoryMetricReader]:
    memory = InMemoryMetricReader()
    metrics.use_meter_provider(MeterProvider(metric_readers=[memory]))
    try:
        yield memory
    finally:
        metrics.use_meter_provider(None)
        metrics.report_queue_health({})


def collected(reader: InMemoryMetricReader) -> dict[str, list[tuple[dict[str, Any], float]]]:
    data = reader.get_metrics_data()
    found: dict[str, list[tuple[dict[str, Any], float]]] = {}
    if data is None:
        return found
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                for point in metric.data.data_points:
                    value = getattr(point, "value", None)
                    if value is None:
                        value = point.sum  # histogram points carry a sum, not a value
                    found.setdefault(metric.name, []).append((dict(point.attributes), value))
    return found


def test_counters_and_gauge_report_through_the_bound_provider(reader: InMemoryMetricReader) -> None:
    instruments = metrics.instruments()
    instruments.commands.add(1, metrics.attributes(command="plan.update", outcome="applied"))
    instruments.commands.add(2, metrics.attributes(command="plan.update", outcome="applied"))
    instruments.push_results.add(1, metrics.attributes(outcome="retry", missing=None))
    instruments.command_duration.record(12.5, metrics.attributes(command="plan.update"))
    metrics.report_queue_health({"failed": 2, "oldest_waiting_seconds": 30.5})

    found = collected(reader)

    assert found["beluno.commands"] == [({"command": "plan.update", "outcome": "applied"}, 3)]
    assert found["beluno.sync.push.results"] == [({"outcome": "retry"}, 1)]
    assert [value for _, value in found["beluno.jobs.queue"]] == [2, 30.5]
    assert "beluno.command.duration" in found


def test_instruments_are_no_ops_without_a_provider() -> None:
    metrics.use_meter_provider(None)
    instruments = metrics.instruments()
    instruments.changes_appended.add(5)
    instruments.pull_pages.add(1, metrics.attributes(scope_type="plan", status="ok"))
    assert metrics.instruments() is instruments
