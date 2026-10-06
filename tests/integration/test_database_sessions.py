"""Application sessions read timestamps in UTC whatever the server's time zone."""

from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import text

from beluno.db.session import Database
from beluno.testkit.environment import IntegrationEnvironment

pytestmark = pytest.mark.integration

LOCAL_ZONE = "Asia/Ho_Chi_Minh"


async def test_sessions_use_utc_when_the_role_defaults_to_another_zone(
    environment: IntegrationEnvironment,
) -> None:
    settings = environment.settings
    assert settings.api_database_dsn is not None
    database_name = environment.admin_dsn.rsplit("/", 1)[1]
    role_zone = sql.SQL("ALTER ROLE api_runtime IN DATABASE {} {}")
    with psycopg.connect(environment.admin_dsn, autocommit=True) as admin:
        admin.execute(
            role_zone.format(
                sql.Identifier(database_name),
                sql.SQL("SET timezone = {}").format(sql.Literal(LOCAL_ZONE)),
            )
        )
    database = Database(settings)
    try:
        plain_dsn = settings.api_database_dsn.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(plain_dsn) as plain:
            assert plain.execute("SHOW TimeZone").fetchone() == (LOCAL_ZONE,)
        async with database.transaction() as session:
            zone = (await session.execute(text("SHOW TimeZone"))).scalar_one()
            stamp = (await session.execute(text("SELECT now()"))).scalar_one()
        assert zone == "UTC"
        assert stamp.utcoffset() == timedelta(0)
    finally:
        await database.close()
        with psycopg.connect(environment.admin_dsn, autocommit=True) as admin:
            admin.execute(
                role_zone.format(sql.Identifier(database_name), sql.SQL("RESET timezone"))
            )
