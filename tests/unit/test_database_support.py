from __future__ import annotations

import pytest

from beluno.config import Settings
from beluno.db.roles import set_actor_context
from beluno.db.session import Database


class RecordingSession:
    def __init__(self) -> None:
        self.parameters: dict[str, str] | None = None

    async def execute(self, _: object, parameters: dict[str, str]) -> None:
        self.parameters = parameters


async def test_unconfigured_database_reports_not_ready_and_rejects_sessions() -> None:
    database = Database(Settings())

    assert database.configured is False
    assert await database.check() is False
    with pytest.raises(RuntimeError, match="not configured"):
        await anext(database.session())
    await database.close()


async def test_database_creates_and_closes_async_engine_without_connecting() -> None:
    database = Database(
        Settings(database_url="postgresql+psycopg://user:password@db.example/beluno")
    )

    assert database.configured is True
    await database.close()


async def test_actor_context_sets_empty_value_when_actor_is_absent() -> None:
    session = RecordingSession()

    await set_actor_context(session, None)  # type: ignore[arg-type]

    assert session.parameters == {"actor_id": ""}
