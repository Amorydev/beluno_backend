"""Activity events: the readable feed of what changed (append-only)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from beluno.db.models.base import Base


class ActivityEvent(Base):
    __tablename__ = "events"
    __table_args__ = {"schema": "activity"}  # noqa: RUF012

    id: Mapped[UUID] = mapped_column(primary_key=True)
    scope_type: Mapped[str] = mapped_column(Text)
    scope_id: Mapped[UUID]
    plan_id: Mapped[UUID | None]
    actor_user_id: Mapped[UUID | None]
    type: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[UUID]
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB)
    occurred_at: Mapped[datetime]
