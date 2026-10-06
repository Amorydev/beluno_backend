"""Group and membership contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from beluno.contracts.common import CurrencyCode, GroupName, TimezoneName

AssignableGroupRole = Literal["admin", "member"]


class GroupCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    name: GroupName
    default_currency: CurrencyCode
    default_timezone: TimezoneName


class GroupUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: GroupName | None = None
    default_currency: CurrencyCode | None = None
    default_timezone: TimezoneName | None = None


class GroupResponse(BaseModel):
    id: UUID
    name: str
    default_currency: str
    default_timezone: str
    state: Literal["active", "deletion_scheduled"]
    deletion_scheduled_at: datetime | None
    my_role: Literal["owner", "admin", "member"] | None
    my_membership_state: Literal["invited", "active", "left", "removed"] | None
    version: int
    created_at: datetime
    updated_at: datetime


class MemberAddRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    role: AssignableGroupRole = "member"


class MemberRoleChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: AssignableGroupRole


class InvitationAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accept: bool


class OwnershipTransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_owner_user_id: UUID


class MemberResponse(BaseModel):
    user_id: UUID
    display_name: str
    role: Literal["owner", "admin", "member"]
    state: Literal["invited", "active", "left", "removed"]
    joined_at: datetime | None
    version: int
