"""Invite link contracts. Tokens travel only in request/response bodies, never URLs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from beluno.contracts.common import DisplayName
from beluno.contracts.iam import EMAIL_PATTERN, DeviceRequest, TokenResponse
from beluno.contracts.plans import ParticipantResponse, PlanKind, PlanResponse, PlanTiming

ExpiresInHours = Field(default=168, ge=1, le=720)


class PlanInviteCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["admin", "member", "viewer"] = "member"
    allow_guests: bool = True
    requires_approval: bool = False
    intended_email: str | None = Field(default=None, max_length=320, pattern=EMAIL_PATTERN)
    max_uses: int | None = Field(default=None, ge=1, le=1_000)
    expires_in_hours: int = ExpiresInHours


class ClaimInviteCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expires_in_hours: int = ExpiresInHours


class InviteResponse(BaseModel):
    id: UUID
    kind: Literal["plan"]
    purpose: Literal["join", "claim"]
    role: str
    allow_guests: bool
    requires_approval: bool
    email_bound: bool
    target_participant_id: UUID | None
    max_uses: int | None
    use_count: int
    expires_at: datetime
    state: Literal["active", "revoked"]
    created_at: datetime


class CreatedInviteResponse(InviteResponse):
    token: str = Field(description="Shown once. Share it inside the invite link; never log it.")


class InviteTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=128)


class RedeemInviteRequest(InviteTokenRequest):
    display_name: DisplayName | None = Field(
        default=None, description="Required when joining as a guest without an account."
    )
    merge_existing: bool = False
    device: DeviceRequest | None = None


class PlanInvitePreview(BaseModel):
    title: str
    kind: PlanKind
    timing: PlanTiming
    organizer_name: str | None


class InvitePreviewResponse(BaseModel):
    """Deliberately minimal: no members, balances, bookings, or locations."""

    kind: Literal["plan"]
    purpose: Literal["join", "claim"]
    requires_approval: bool
    allow_guests: bool
    placeholder_name: str | None
    expires_at: datetime
    plan: PlanInvitePreview


class RedeemInviteResponse(BaseModel):
    kind: Literal["plan"]
    status: Literal["active", "pending_approval"]
    plan: PlanResponse | None = Field(
        default=None, description="Present once access is active (not while pending approval)."
    )
    participant: ParticipantResponse | None = None
    session: TokenResponse | None = Field(
        default=None, description="Guest session created for a caller without an account."
    )
