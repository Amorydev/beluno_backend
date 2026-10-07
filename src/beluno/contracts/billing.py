"""Purchases, entitlements, and store notifications."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ApplePurchaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signed_transaction: str = Field(
        min_length=1,
        max_length=20_000,
        description="StoreKit 2 `Transaction.jwsRepresentation`; set `appAccountToken` to the "
        "user id when buying",
    )
    plan_id: UUID | None = Field(default=None, description="The trip a Trip Pass unlocks")


class GooglePurchaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(min_length=1, max_length=200)
    purchase_token: str = Field(
        min_length=1,
        max_length=4096,
        description="Set `obfuscatedAccountId` to the user id when buying",
    )
    plan_id: UUID | None = Field(default=None, description="The trip a Trip Pass unlocks")


class PurchaseResponse(BaseModel):
    id: UUID
    store: Literal["apple", "google"]
    product: Literal["trip_pass", "pro"]
    plan_id: UUID | None
    purchased_at: datetime
    expires_at: datetime | None
    active: bool


class ProResponse(BaseModel):
    store: Literal["apple", "google"]
    expires_at: datetime


class EntitlementsResponse(BaseModel):
    pro: ProResponse | None
    active_trips: int = Field(description="Your own trips in progress without a Trip Pass")
    active_trip_limit: int | None = Field(description="None: no limit (Pro, or not enforced)")
    receipts_per_trip: int | None = Field(description="For trips without a Trip Pass or Pro")
    trip_pass_product_ids: list[str]
    pro_product_ids: list[str]


class PlanEntitlementResponse(BaseModel):
    unlocked_by: Literal["trip_pass", "pro"] | None
    receipts: int
    receipt_limit: int | None


class AppleNotificationRequest(BaseModel):
    signedPayload: str = Field(min_length=1, max_length=100_000)


class PubSubMessage(BaseModel):
    data: str = Field(default="", max_length=100_000)
    messageId: str | None = None


class GooglePushRequest(BaseModel):
    message: PubSubMessage
    subscription: str | None = None
