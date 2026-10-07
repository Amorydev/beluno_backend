"""People: a user's saved crews of people they plan with."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import ARRAY, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from beluno.db.models.base import Base


class Crew(Base):
    __tablename__ = "crews"
    __table_args__ = {"schema": "people"}  # noqa: RUF012

    id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_user_id: Mapped[UUID]
    name: Mapped[str] = mapped_column(Text)
    member_user_ids: Mapped[list[UUID]] = mapped_column(ARRAY(Uuid(as_uuid=True)))
    source_plan_id: Mapped[UUID | None]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    deleted_at: Mapped[datetime | None]
