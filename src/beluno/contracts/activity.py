"""Activity feed contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ActivityEventResponse(BaseModel):
    """One thing that happened; join the IDs with synced entities to render it."""

    id: UUID
    type: str = Field(description="For example expense.added, member.left, plan.state_changed")
    entity_type: str
    entity_id: UUID
    plan_id: UUID | None
    actor_user_id: UUID | None
    summary: dict[str, Any] = Field(
        description="IDs, amounts, currencies, roles, states, dates, and field names; never "
        "free text"
    )
    occurred_at: datetime
