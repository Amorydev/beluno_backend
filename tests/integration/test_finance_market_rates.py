"""Market rate estimates: the worker stores a provider's rates, the API serves the latest."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import psycopg
import pytest

from beluno.auth import AccessTokenCodec
from beluno.config import Settings
from beluno.db.session import Database
from beluno.modules.context import Runtime
from beluno.modules.finance.market_rates import (
    NoRateProvider,
    PublishedRate,
    ingest_market_rates,
)
from beluno.modules.iam.external_identity import ExternalIdentityVerifier
from beluno.testkit.api_client import sign_in
from beluno.testkit.identity import IdentityProviderStub
from beluno.token_hashing import TokenHasher
from beluno.worker import tasks

pytestmark = pytest.mark.integration

YESTERDAY = datetime(2026, 10, 5, 16, 0, tzinfo=UTC)
TODAY = datetime(2026, 10, 6, 16, 0, tzinfo=UTC)


class PublishedRates:
    """A provider adapter that returns fixed publications (the port's test double)."""

    def __init__(self, rates: list[PublishedRate]) -> None:
        self.rates = rates

    async def latest(self) -> list[PublishedRate]:
        return self.rates


def published(quote: str, rate: str, as_of: datetime, base: str = "JPY") -> PublishedRate:
    return PublishedRate(base, quote, Decimal(rate), as_of, "test-feed")


@pytest.fixture
async def worker(live_settings: Settings) -> AsyncIterator[Runtime]:
    database = Database.for_worker(live_settings)
    yield Runtime(
        settings=live_settings,
        database=database,
        tokens=AccessTokenCodec(live_settings),
        hasher=TokenHasher.from_settings(live_settings),
        identity_verifier=ExternalIdentityVerifier(live_settings),
    )
    await database.close()


async def test_the_worker_stores_new_rates_and_the_api_serves_the_latest(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    worker: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed = PublishedRates(
        [
            published("VND", "168.40", YESTERDAY),
            published("VND", "168.52", TODAY),
            published("USD", "0.0067", TODAY),
            published("USD", "0.0067", TODAY),  # repeated in one publication
            published("XYZ", "1.5", TODAY),  # unsupported currency
            published("EUR", "-1", TODAY),  # not a rate
            published("JPY", "1", TODAY),  # same currency
        ]
    )
    assert await ingest_market_rates(worker, feed) == 3
    assert await ingest_market_rates(worker, feed) == 0
    assert await ingest_market_rates(worker, NoRateProvider()) == 0
    # The scheduled job runs the no-op adapter until a provider is chosen.
    monkeypatch.setattr(tasks, "get_worker_runtime", lambda: worker)
    assert await tasks.ingest_finance_market_rates(0) == 0

    person = await sign_in(api, identity_provider, name="Linh")
    rates = await api.get("/v1/fx/rates", params={"base": "JPY"}, headers=person.headers)
    assert rates.status_code == 200, rates.text
    body = rates.json()
    assert (body["base"], body["estimate_only"]) == ("JPY", True)
    assert [(row["quote"], row["rate"], row["as_of"]) for row in body["rates"]] == [
        ("USD", "0.0067", "2026-10-06T16:00:00Z"),
        ("VND", "168.52", "2026-10-06T16:00:00Z"),
    ]
    empty = await api.get("/v1/fx/rates", params={"base": "VND"}, headers=person.headers)
    assert empty.json()["rates"] == []
    lowercase = await api.get("/v1/fx/rates", params={"base": "vnd"}, headers=person.headers)
    assert lowercase.status_code == 422


async def test_only_the_worker_writes_rates(live_settings: Settings) -> None:
    assert live_settings.api_database_dsn is not None
    dsn = live_settings.api_database_dsn.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn) as connection, pytest.raises(psycopg.errors.InsufficientPrivilege):
        connection.execute(
            "INSERT INTO finance.market_rates (base_currency, quote_currency, rate, as_of, "
            "source, fetched_at) VALUES ('JPY', 'VND', 1, now(), 'api', now())"
        )
