from __future__ import annotations

import pytest

from beluno.config import Settings
from beluno.worker.connection import worker_dsn


def test_worker_dsn_converts_sqlalchemy_psycopg_scheme() -> None:
    settings = Settings(api_database_url="postgresql+psycopg://user:password@db.example/beluno")

    assert worker_dsn(settings) == "postgresql://user:password@db.example/beluno"


def test_worker_dsn_requires_database_configuration() -> None:
    with pytest.raises(RuntimeError, match="BELUNO_WORKER_DATABASE_URL"):
        worker_dsn(Settings())
