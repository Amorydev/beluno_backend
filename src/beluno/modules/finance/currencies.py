"""Pinned currency metadata (``finance.currencies``)."""

from __future__ import annotations

from sqlalchemy import select

from beluno.db.models.finance import Currency
from beluno.modules.context import CommandContext
from beluno.modules.finance.errors import currency_not_supported

SUPPORTED = "supported"


async def list_currencies(ctx: CommandContext) -> list[Currency]:
    rows = await ctx.session.execute(select(Currency).order_by(Currency.code))
    return list(rows.scalars())


async def supported_currency(ctx: CommandContext, code: str) -> Currency:
    currency = await ctx.session.get(Currency, code)
    if currency is None or currency.state != SUPPORTED:
        raise currency_not_supported(code)
    return currency


async def require_supported_currency(ctx: CommandContext, code: str) -> None:
    await supported_currency(ctx, code)
