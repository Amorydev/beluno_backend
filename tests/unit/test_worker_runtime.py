from __future__ import annotations

import pytest

from beluno.config import Settings
from beluno.scheduler import main as scheduler_main
from beluno.worker import main as worker_main
from beluno.worker.tasks import heartbeat


async def test_heartbeat_task_is_idempotent_for_its_payload() -> None:
    assert await heartbeat.func("release-1", payload_version=1) == "release-1"


async def test_heartbeat_rejects_unknown_payload_versions() -> None:
    with pytest.raises(ValueError, match="Unsupported heartbeat payload version"):
        await heartbeat.func("release-1", payload_version=2)


async def test_worker_fails_without_database_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_main, "get_settings", lambda: Settings())

    with pytest.raises(RuntimeError, match="BELUNO_WORKER_DATABASE_URL"):
        await worker_main.run()


async def test_scheduler_fails_without_database_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scheduler_main, "get_settings", lambda: Settings())

    with pytest.raises(RuntimeError, match="BELUNO_SCHEDULER_DATABASE_URL"):
        await scheduler_main.run_once()
