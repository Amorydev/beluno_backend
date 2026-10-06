"""Direct superuser access for test setup and assertions that bypass the API."""

from __future__ import annotations

from typing import Any

import psycopg


class AdminDatabase:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def execute(self, statement: str, *params: object) -> None:
        with psycopg.connect(self._dsn, autocommit=True) as connection:
            connection.execute(statement, params)

    def fetch(self, statement: str, *params: object) -> list[tuple[Any, ...]]:
        with psycopg.connect(self._dsn) as connection:
            return list(connection.execute(statement, params).fetchall())

    def scalar(self, statement: str, *params: object) -> Any:
        rows = self.fetch(statement, *params)
        return rows[0][0] if rows else None
