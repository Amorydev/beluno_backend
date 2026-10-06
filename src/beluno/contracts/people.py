"""Crew contracts: a user's private, saved lists of people they plan with."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from beluno.contracts.common import clean_text

CrewName = Annotated[
    str, AfterValidator(clean_text), StringConstraints(min_length=1, max_length=60)
]
MemberIds = Annotated[list[UUID], Field(min_length=1, max_length=50)]


def _unique(ids: list[UUID] | None) -> None:
    if ids is not None and len(set(ids)) != len(ids):
        raise ValueError("member_user_ids must not repeat a person")


class CrewCreateRequest(BaseModel):
    """Name the people directly, or save a plan's registered people ("Saved from Friday hotpot").

    Registered accounts only; guests are never listed and join plans through invite links.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    name: CrewName
    member_user_ids: MemberIds | None = None
    from_plan_id: UUID | None = None

    @model_validator(mode="after")
    def one_source(self) -> Self:
        if (self.member_user_ids is None) == (self.from_plan_id is None):
            raise ValueError("send member_user_ids or from_plan_id")
        _unique(self.member_user_ids)
        return self


class CrewUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: CrewName | None = None
    member_user_ids: MemberIds | None = None

    @model_validator(mode="after")
    def something_changes(self) -> Self:
        if self.name is None and self.member_user_ids is None:
            raise ValueError("send a new name or member list")
        _unique(self.member_user_ids)
        return self


class CrewMemberResponse(BaseModel):
    user_id: UUID
    display_name: str | None = Field(description="Null once the person shares no plan with you")
    addable: bool = Field(
        description="A registered person you share an active plan with: a new plan can add "
        "them directly. Others join through an invite link."
    )


class CrewResponse(BaseModel):
    id: UUID
    name: str
    members: list[CrewMemberResponse]
    source_plan_id: UUID | None
    version: int
    created_at: datetime
    updated_at: datetime
