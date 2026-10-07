"""Purchases (Trip Pass, Pro), what the caller and a trip are entitled to, and the
stores' notifications about renewals and refunds.

Restoring purchases is sending them again: both purchase routes are safe to repeat.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response, status

from beluno.api.dependencies import ActorDep, RuntimeDep, client_subject
from beluno.api.problems import problem_responses
from beluno.auth import AuthenticatedActor
from beluno.contracts.billing import (
    AppleNotificationRequest,
    ApplePurchaseRequest,
    EntitlementsResponse,
    GooglePurchaseRequest,
    GooglePushRequest,
    PlanEntitlementResponse,
    ProResponse,
    PurchaseResponse,
)
from beluno.contracts.errors import BelunoError, authentication_failed, conflict, validation_error
from beluno.modules import billing
from beluno.modules.context import Runtime, open_context
from beluno.modules.iam import rate_limits
from beluno.stores import PurchaseInvalid, PurchasePending, StoreUnavailable, VerifiedPurchase

router = APIRouter(tags=["billing"])
Store = Literal["apple", "google"]
Unlock = Literal["trip_pass", "pro"] | None

PURCHASE_ERRORS = problem_responses(401, 403, 404, 409, 422, 429, 503)


@router.get(
    "/v1/me/entitlements",
    response_model=EntitlementsResponse,
    responses=problem_responses(401, 503),
)
async def get_entitlements(runtime: RuntimeDep, actor: ActorDep) -> EntitlementsResponse:
    """Pro, the free limits, and the store product ids to sell."""

    async with open_context(runtime, actor) as ctx:
        found = await billing.entitlements(ctx)
    settings = runtime.settings
    return EntitlementsResponse(
        pro=ProResponse(store=cast(Store, found.pro.store), expires_at=found.pro.expires_at)
        if found.pro is not None and found.pro.expires_at is not None
        else None,
        active_trips=found.active_trips,
        active_trip_limit=None if found.pro is not None else found.active_trip_limit,
        receipts_per_trip=found.receipts_per_trip,
        trip_pass_product_ids=settings.store_trip_pass_product_ids,
        pro_product_ids=settings.store_pro_product_ids,
    )


@router.get(
    "/v1/plans/{plan_id}/entitlement",
    response_model=PlanEntitlementResponse,
    responses=problem_responses(401, 403, 404, 503),
)
async def get_plan_entitlement(
    plan_id: UUID, runtime: RuntimeDep, actor: ActorDep
) -> PlanEntitlementResponse:
    """Whether a Trip Pass or the owner's Pro unlocks the trip, and its receipt limit."""

    async with open_context(runtime, actor) as ctx:
        found = await billing.plan_entitlement(ctx, plan_id)
    return PlanEntitlementResponse(
        unlocked_by=cast(Unlock, found.unlocked_by),
        receipts=found.receipts,
        receipt_limit=found.receipt_limit,
    )


@router.post("/v1/me/purchases/apple", response_model=PurchaseResponse, responses=PURCHASE_ERRORS)
async def record_apple_purchase(
    body: ApplePurchaseRequest, runtime: RuntimeDep, actor: ActorDep
) -> PurchaseResponse:
    """Record an App Store purchase (also to restore one); safe to repeat."""

    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.PURCHASES_PER_USER, str(actor.user_id)
    )
    with _store_errors():
        purchase = await runtime.apple_store.transaction(body.signed_transaction)
    return await _record(runtime, actor, purchase, body.plan_id)


@router.post("/v1/me/purchases/google", response_model=PurchaseResponse, responses=PURCHASE_ERRORS)
async def record_google_purchase(
    body: GooglePurchaseRequest, runtime: RuntimeDep, actor: ActorDep
) -> PurchaseResponse:
    """Record a Google Play purchase (also to restore one); safe to repeat."""

    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.PURCHASES_PER_USER, str(actor.user_id)
    )
    with _store_errors():
        purchase = await runtime.google_play.purchase(body.product_id, body.purchase_token)
    return await _record(runtime, actor, purchase, body.plan_id)


@router.post(
    "/v1/store-notifications/apple",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=problem_responses(422, 429, 503),
)
async def apple_notification(
    body: AppleNotificationRequest, request: Request, runtime: RuntimeDep
) -> Response:
    """App Store Server Notifications (version 2): renewals, expiries, refunds."""

    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.STORE_NOTIFICATIONS_PER_CLIENT, client_subject(request)
    )
    with _store_errors():
        purchase = await runtime.apple_store.notification(body.signedPayload)
    if purchase is not None:
        await _apply(runtime, billing.update_from(purchase))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/v1/store-notifications/google",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=problem_responses(401, 422, 429, 503),
)
async def google_notification(
    body: GooglePushRequest,
    request: Request,
    runtime: RuntimeDep,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    """Google Play real-time developer notifications, pushed by Pub/Sub."""

    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.STORE_NOTIFICATIONS_PER_CLIENT, client_subject(request)
    )
    with _store_errors():
        play = runtime.google_play
        if not await play.push_is_authentic(authorization):
            raise authentication_failed()
        update = await billing.google_update(
            runtime.settings, play, _push_data(body.message.data), runtime.clock()
        )
    if update is not None:
        await _apply(runtime, update)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _record(
    runtime: Runtime, actor: AuthenticatedActor, purchase: VerifiedPurchase, plan_id: UUID | None
) -> PurchaseResponse:
    async with open_context(runtime, actor) as ctx:
        recorded = await billing.record_purchase(ctx, purchase, plan_id)
        now = ctx.now
    return PurchaseResponse(
        id=recorded.id,
        store=cast(Store, purchase.store),
        product=purchase.product.value,
        plan_id=recorded.plan_id,
        purchased_at=purchase.purchased_at,
        expires_at=recorded.expires_at,
        active=recorded.active(now),
    )


async def _apply(runtime: Runtime, update: billing.StoreUpdate) -> None:
    async with open_context(runtime) as ctx:
        await billing.apply_store_update(ctx, update)


def _push_data(data: str) -> dict[str, Any]:
    try:
        decoded = json.loads(base64.b64decode(data, validate=True))
    except (binascii.Error, ValueError):
        raise validation_error("the message data is not base64 JSON") from None
    if not isinstance(decoded, dict):
        raise validation_error("the message data is not a JSON object")
    return decoded


@contextmanager
def _store_errors() -> Iterator[None]:
    """Store answers as problems: forged or unknown 422, pending 409, unavailable 503."""

    try:
        yield
    except PurchaseInvalid as error:
        raise BelunoError(
            status=422,
            code="PURCHASE_INVALID",
            title="The store does not vouch for this purchase",
            detail=str(error),
        ) from error
    except PurchasePending as error:
        raise conflict(
            "PURCHASE_PENDING", "Payment pending", "Nothing unlocks until the store confirms it"
        ) from error
    except StoreUnavailable as error:
        raise BelunoError(
            status=503,
            code="STORE_UNAVAILABLE",
            title="The store is temporarily unavailable",
            detail=str(error),
        ) from error
