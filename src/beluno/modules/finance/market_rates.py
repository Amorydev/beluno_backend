"""Market rate estimates: a daily reference feed for offline estimates only.

Nothing in the ledger reads these rates. A client shows them as "market · est."
and the person accepts one or types their own; the expense or settlement then
keeps its own immutable snapshot. The provider is a port. Until one is chosen
the no-op adapter publishes nothing and the endpoint returns an empty list.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on

from beluno.contracts.errors import BelunoError
from beluno.db.models.finance import Currency, MarketRate
from beluno.modules.context import CommandContext, Runtime
from beluno.modules.finance.currencies import SUPPORTED
from beluno.modules.finance.fx import parse_rate


@dataclass(frozen=True)
class PublishedRate:
    """Quote-currency units for one base-currency unit, as a provider published it."""

    base_currency: str
    quote_currency: str
    rate: Decimal
    as_of: datetime
    source: str


class RateProvider(Protocol):
    async def latest(self) -> list[PublishedRate]: ...


class NoRateProvider:
    """No provider is configured yet (ECB reference rates lack VND)."""

    async def latest(self) -> list[PublishedRate]:
        return []


def rate_provider() -> RateProvider:
    return NoRateProvider()


async def ingest_market_rates(runtime: Runtime, provider: RateProvider) -> int:
    """Fetch outside any transaction, then store the rates not stored yet; returns rows added."""

    published = await provider.latest()
    if not published:
        return 0
    added = 0
    async with runtime.database.transaction() as session:
        supported = set(
            (
                await session.execute(select(Currency.code).where(Currency.state == SUPPORTED))
            ).scalars()
        )
        seen: set[tuple[str, str, datetime]] = set()
        for item in published:
            key = (item.base_currency, item.quote_currency, item.as_of)
            if (
                key in seen
                or item.base_currency == item.quote_currency
                or not {item.base_currency, item.quote_currency} <= supported
                or not 1 <= len(item.source) <= 40
            ):
                continue
            seen.add(key)
            try:
                rate = parse_rate(item.rate)
            except BelunoError:
                continue
            if await session.get(MarketRate, key) is not None:
                continue
            session.add(
                MarketRate(
                    base_currency=item.base_currency,
                    quote_currency=item.quote_currency,
                    as_of=item.as_of,
                    rate=rate,
                    source=item.source,
                    fetched_at=runtime.clock(),
                )
            )
            added += 1
        await session.flush()
    return added


async def latest_rates(ctx: CommandContext, base_currency: str) -> list[MarketRate]:
    """The newest published rate from ``base_currency`` to every quote currency."""

    rows = await ctx.session.execute(
        select(MarketRate)
        .where(MarketRate.base_currency == base_currency)
        .ext(distinct_on(MarketRate.quote_currency))
        .order_by(MarketRate.quote_currency, MarketRate.as_of.desc())
    )
    return list(rows.scalars())
