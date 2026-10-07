"""Paid plans: purchases, what they unlock, and the free limits.

A Trip Pass unlocks one trip for everyone on it, whoever bought it; Pro (yearly)
unlocks every trip its holder owns. Unlocked trips have no receipt limit and do not
count toward the free limit on a person's own trips in progress. Hangouts are always
free. Limits apply only once their settings are set. Purchases are verified with the
store before any transaction opens (``beluno.stores``); the database records them
through definer functions, so the API never writes purchase rows itself.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text

from beluno.authorization.access import load_plan, require_plan
from beluno.authorization.policy import PlanAction
from beluno.config import Settings
from beluno.contracts.errors import conflict, forbidden, validation_error
from beluno.db.ids import new_id
from beluno.db.models.billing import Purchase
from beluno.db.models.media import Media
from beluno.modules.context import CommandContext, Runtime
from beluno.modules.sync_audit.recorder import record_audit
from beluno.stores import (
    GOOGLE,
    GooglePlay,
    PurchaseInvalid,
    PurchasePending,
    StoreProduct,
    VerifiedPurchase,
)
from beluno.worker.enqueue import defer_in_transaction

TRIP = "trip"
RECEIPT = "receipt"
ACKNOWLEDGE_TASK = "billing.acknowledge_purchase"
BILLING_QUEUE = "maintenance"
# Google: the subscription was refunded and ended at once; a voided one-time product.
SUBSCRIPTION_REVOKED = 12
VOIDED_ONE_TIME = 2

RECORD = text(
    "SELECT outcome, purchase_id, plan_id, expires_at, revoked_at FROM billing.record_purchase("
    ":id, :store, :product, :product_id, :original_id, :plan_id, :environment,"
    " :purchased_at, :expires_at, :revoked_at, :replaces)"
)
UPDATE = text("SELECT billing.update_purchase(:store, :original_id, :expires_at, :revoked_at)")
PLAN_UNLOCK = text("SELECT billing.plan_unlock(:plan_id, :now)")
TRIPS_COUNTING = text("SELECT billing.trips_counting(:owner, :except_id, :now)")
AWAITING = text(
    "SELECT product, product_id, original_id FROM billing.awaiting_acknowledgement(:id)"
)
ACKNOWLEDGED = text("SELECT billing.acknowledged(:id, :now)")
# One person's trip-limit checks run one at a time (two new trips at once stay honest).
LOCK_TRIPS = text("SELECT pg_advisory_xact_lock(hashtextextended('billing:trips:' || :user_id, 0))")


@dataclass(frozen=True)
class Entitlements:
    pro: Purchase | None
    active_trips: int
    active_trip_limit: int | None
    receipts_per_trip: int | None


@dataclass(frozen=True)
class PlanEntitlement:
    unlocked_by: str | None  # "trip_pass", "pro", or None
    receipts: int
    receipt_limit: int | None


@dataclass(frozen=True)
class RecordedPurchase:
    id: UUID
    purchase: VerifiedPurchase
    # As stored, after merging what the store said before.
    plan_id: UUID | None
    expires_at: datetime | None
    revoked_at: datetime | None

    def active(self, now: datetime) -> bool:
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > now)


async def entitlements(ctx: CommandContext) -> Entitlements:
    counting = await _trips_counting(ctx, ctx.require_actor().user_id, None)
    return Entitlements(
        pro=await active_pro(ctx),
        active_trips=counting or 0,  # with Pro nothing counts
        active_trip_limit=None if counting is None else ctx.settings.free_active_trips,
        receipts_per_trip=ctx.settings.media_receipts_per_plan,
    )


async def active_pro(ctx: CommandContext) -> Purchase | None:
    """The caller's Pro subscription that runs the longest, if one is active."""

    return (
        await ctx.session.execute(
            select(Purchase)
            .where(
                Purchase.user_id == ctx.require_actor().user_id,
                Purchase.product == StoreProduct.PRO.value,
                Purchase.revoked_at.is_(None),
                Purchase.superseded_at.is_(None),
                Purchase.expires_at > ctx.now,
            )
            .order_by(Purchase.expires_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def plan_entitlement(ctx: CommandContext, plan_id: UUID) -> PlanEntitlement:
    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW)
    unlocked_by = await ctx.session.scalar(PLAN_UNLOCK, {"plan_id": plan_id, "now": ctx.now})
    return PlanEntitlement(
        unlocked_by=unlocked_by,
        receipts=await _receipts(ctx, plan_id),
        receipt_limit=await receipt_limit(ctx, plan_id, access.plan.type),
    )


async def receipt_limit(ctx: CommandContext, plan_id: UUID, plan_type: str) -> int | None:
    """How many receipts the plan may hold; None for no limit."""

    limit = ctx.settings.media_receipts_per_plan
    if limit is None or plan_type != TRIP:
        return None
    unlocked = await ctx.session.scalar(PLAN_UNLOCK, {"plan_id": plan_id, "now": ctx.now})
    return None if unlocked else limit


async def require_room_for_trip(
    ctx: CommandContext, owner_id: UUID, plan_id: UUID | None = None
) -> None:
    """Before a person's own trip starts, reopens, comes back, or passes to them.

    ``owner_id`` owns the trip (or is about to); ``plan_id`` is the trip itself, which is
    never counted against itself and needs no place when its Trip Pass unlocks it.
    """

    limit = ctx.settings.free_active_trips
    if limit is None:
        return
    await ctx.session.execute(LOCK_TRIPS, {"user_id": str(owner_id)})
    if plan_id is not None and (
        await ctx.session.scalar(PLAN_UNLOCK, {"plan_id": plan_id, "now": ctx.now})
        == StoreProduct.TRIP_PASS.value
    ):
        return
    counting = await _trips_counting(ctx, owner_id, plan_id)
    if counting is not None and counting >= limit:
        raise conflict(
            "TRIP_LIMIT_REACHED",
            "Trip limit reached",
            f"Free accounts have {limit} trips of their own in progress at a time; finish "
            "one, or unlock trips with a Trip Pass or Pro",
        )


async def _trips_counting(ctx: CommandContext, owner_id: UUID, plan_id: UUID | None) -> int | None:
    """The owner's trips counting toward the free limit; None when Pro lifts it."""

    found = await ctx.session.scalar(
        TRIPS_COUNTING, {"owner": owner_id, "except_id": plan_id, "now": ctx.now}
    )
    return None if found is None else int(found)


async def record_purchase(
    ctx: CommandContext, purchase: VerifiedPurchase, plan_id: UUID | None
) -> RecordedPurchase:
    """Record a purchase the store vouched for; replays are safe and update it."""

    actor = ctx.require_actor()
    if actor.is_guest:
        raise forbidden("Sign in with an account to buy")
    if purchase.account is not None and purchase.account != actor.user_id:
        raise _owned_elsewhere()
    if (purchase.product is StoreProduct.TRIP_PASS) != (plan_id is not None):
        raise validation_error("a Trip Pass names the trip it unlocks, and only a Trip Pass")
    if plan_id is not None:
        access = await load_plan(ctx, plan_id)
        require_plan(access, PlanAction.VIEW)
        if access.plan.type != TRIP:
            raise conflict("NOT_AVAILABLE_FOR_HANGOUT", "Hangouts are always free")
    row = (
        await ctx.session.execute(
            RECORD,
            {
                "id": new_id(),
                "store": purchase.store,
                "product": purchase.product.value,
                "product_id": purchase.product_id,
                "original_id": purchase.original_id,
                "plan_id": plan_id,
                "environment": purchase.environment,
                "purchased_at": purchase.purchased_at,
                "expires_at": purchase.expires_at,
                "revoked_at": purchase.revoked_at,
                "replaces": purchase.replaces,
            },
        )
    ).one()
    if row.outcome == "owned_elsewhere":
        raise _owned_elsewhere()
    if row.outcome == "used_elsewhere":
        raise conflict(
            "PURCHASE_USED", "Purchase already used", "This Trip Pass unlocks another trip"
        )
    if row.outcome == "recorded":
        await record_audit(
            ctx,
            action="billing.purchase_recorded",
            entity_type="purchase",
            entity_id=row.purchase_id,
            metadata={"store": purchase.store, "product": purchase.product.value},
            plan_id=plan_id,
        )
    if purchase.store == GOOGLE and purchase.revoked_at is None:
        await defer_in_transaction(
            ctx.session,
            task_name=ACKNOWLEDGE_TASK,
            queue=BILLING_QUEUE,
            args={"purchase_id": str(row.purchase_id)},
            queueing_lock=f"billing:acknowledge:{row.purchase_id}",
        )
    return RecordedPurchase(
        id=row.purchase_id,
        purchase=purchase,
        plan_id=row.plan_id,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )


@dataclass(frozen=True)
class StoreUpdate:
    store: str
    original_id: str
    expires_at: datetime | None
    revoked_at: datetime | None


def update_from(purchase: VerifiedPurchase) -> StoreUpdate:
    return StoreUpdate(
        purchase.store, purchase.original_id, purchase.expires_at, purchase.revoked_at
    )


async def google_update(
    settings: Settings, play: GooglePlay, payload: dict[str, Any], now: datetime
) -> StoreUpdate | None:
    """What a Google real-time developer notification changes (asks Google; no transaction)."""

    if payload.get("packageName") != settings.google_play_package_name:
        return None
    voided = payload.get("voidedPurchaseNotification")
    if isinstance(voided, dict):
        # A refunded one-time product ends; a subscription's refunded renewal does not
        # end it by itself (Google sends SUBSCRIPTION_REVOKED when it does).
        if voided.get("productType") == VOIDED_ONE_TIME and voided.get("purchaseToken"):
            return StoreUpdate(GOOGLE, str(voided["purchaseToken"]), None, now)
        return None
    subscription = payload.get("subscriptionNotification")
    one_time = payload.get("oneTimeProductNotification")
    if isinstance(subscription, dict):
        token, product_id = subscription.get("purchaseToken"), subscription.get("subscriptionId")
        if subscription.get("notificationType") == SUBSCRIPTION_REVOKED and token:
            return StoreUpdate(GOOGLE, str(token), None, now)
    elif isinstance(one_time, dict):
        token, product_id = one_time.get("purchaseToken"), one_time.get("sku")
    else:
        return None  # a test notification
    if not token or not product_id:
        return None
    try:
        return update_from(await play.purchase(str(product_id), str(token)))
    except (PurchaseInvalid, PurchasePending):
        return None


async def apply_store_update(ctx: CommandContext, update: StoreUpdate) -> bool:
    """A store notification about a purchase: a renewal, an expiry, or a refund."""

    changed = await ctx.session.scalar(
        UPDATE,
        {
            "store": update.store,
            "original_id": update.original_id,
            "expires_at": update.expires_at,
            "revoked_at": update.revoked_at,
        },
    )
    if changed is None:
        return False
    await record_audit(
        ctx,
        action="billing.purchase_updated",
        entity_type="purchase",
        entity_id=changed,
        metadata={"store": update.store, "revoked": update.revoked_at is not None},
    )
    return True


async def acknowledge_purchase(runtime: Runtime, play: GooglePlay, purchase_id: UUID) -> bool:
    """Worker: confirm a Google purchase once (store I/O outside any transaction)."""

    async with runtime.database.transaction() as session:
        row = (await session.execute(AWAITING, {"id": purchase_id})).one_or_none()
    if row is None:
        return False
    # Already consumed or acknowledged (by an app that should not have): nothing left.
    with suppress(PurchaseInvalid):
        await play.acknowledge(StoreProduct(row.product), row.product_id, row.original_id)
    async with runtime.database.transaction() as session:
        await session.execute(ACKNOWLEDGED, {"id": purchase_id, "now": runtime.clock()})
    return True


async def _receipts(ctx: CommandContext, plan_id: UUID) -> int:
    count = await ctx.session.scalar(
        select(func.count())
        .select_from(Media)
        .where(Media.plan_id == plan_id, Media.kind == RECEIPT, Media.deleted_at.is_(None))
    )
    return int(count or 0)


def _owned_elsewhere() -> Exception:
    return conflict(
        "PURCHASE_OWNED_ELSEWHERE",
        "Purchase belongs to another account",
        "Sign in with the account that made this purchase",
    )
