"""Immutable FX snapshots recorded alongside the entries that use them."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from beluno.db.ids import new_id
from beluno.db.models.finance import FxSnapshot
from beluno.modules.finance.errors import base_currency_changed
from beluno.modules.finance.fx import RateSource
from beluno.modules.finance.ledger import Ledger


@dataclass(frozen=True)
class RateInput:
    rate: str
    source: RateSource
    as_of: datetime | None
    # The base currency the caller converted into; it must still be the plan's.
    base_currency: str | None = None


def require_current_base(rate: RateInput, base_currency: str) -> None:
    """A rate made for an older base currency must never be read as one for the new base."""

    if rate.base_currency is not None and rate.base_currency != base_currency:
        raise base_currency_changed()


async def record_rate(
    ledger: Ledger,
    *,
    base: str,
    quote: str,
    rate: Decimal,
    source: RateSource,
    as_of: datetime | None,
) -> FxSnapshot:
    ctx = ledger.ctx
    snapshot = FxSnapshot(
        id=new_id(),
        plan_id=ledger.plan_id,
        base_currency=base,
        quote_currency=quote,
        rate=rate,
        source=source.value,
        rounding_mode="half_even",
        as_of=as_of or ctx.now,
        created_by_user_id=ctx.require_actor().user_id,
        created_at=ctx.now,
    )
    ctx.session.add(snapshot)
    await ctx.session.flush()
    return snapshot
