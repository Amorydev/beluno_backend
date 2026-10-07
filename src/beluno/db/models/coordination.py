"""Trip planning: tasks and packing lists."""

from __future__ import annotations

from datetime import date, datetime, time
from uuid import UUID

from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from beluno.db.models.base import Base

SCHEMA = {"schema": "coordination"}


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    title: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    assignee_participant_id: Mapped[UUID | None]
    due_date: Mapped[date | None]
    due_time: Mapped[time | None]
    due_timezone: Mapped[str | None] = mapped_column(Text)
    remind_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(Text)
    completed_at: Mapped[datetime | None]
    completed_by_user_id: Mapped[UUID | None]
    item_id: Mapped[UUID | None]
    booking_id: Mapped[UUID | None]
    created_by_user_id: Mapped[UUID]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    deleted_at: Mapped[datetime | None]


class PackingItem(Base):
    __tablename__ = "packing_items"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    visibility: Mapped[str] = mapped_column(Text)
    owner_user_id: Mapped[UUID | None]
    name: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    quantity: Mapped[int]
    bringer_participant_id: Mapped[UUID | None]
    packed: Mapped[bool]
    template_id: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[UUID]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    deleted_at: Mapped[datetime | None]


class TemplateApplication(Base):
    """A packing template applied once per plan, owner (None: shared), and template."""

    __tablename__ = "template_applications"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    template_id: Mapped[str] = mapped_column(Text)
    owner_user_id: Mapped[UUID | None]
    applied_by_user_id: Mapped[UUID]
    applied_at: Mapped[datetime]
