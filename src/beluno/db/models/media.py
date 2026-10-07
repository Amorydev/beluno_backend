"""Media: uploaded files of a plan (receipts, covers, memories) and objects to delete."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, Text
from sqlalchemy.orm import Mapped, mapped_column

from beluno.db.models.base import Base

SCHEMA = {"schema": "media_memories"}


class Media(Base):
    __tablename__ = "media"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    kind: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    rejection: Mapped[str | None] = mapped_column(Text)
    declared_type: Mapped[str] = mapped_column(Text)
    declared_size: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    width: Mapped[int | None]
    height: Mapped[int | None]
    expense_id: Mapped[UUID | None]
    uploaded_by_user_id: Mapped[UUID]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    deleted_at: Mapped[datetime | None]


class ObjectDeletion(Base):
    __tablename__ = "object_deletions"
    __table_args__ = SCHEMA

    object_key: Mapped[str] = mapped_column(Text, primary_key=True)
    requested_at: Mapped[datetime]
