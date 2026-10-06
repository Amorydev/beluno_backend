"""Reusable groups and their current memberships."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import LargeBinary, Text
from sqlalchemy.orm import Mapped, mapped_column

from beluno.db.models.base import Base


class Group(Base):
    __tablename__ = "groups"
    __table_args__ = {"schema": "groups"}  # noqa: RUF012

    id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    default_currency: Mapped[str] = mapped_column(Text)
    default_timezone: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    deletion_scheduled_at: Mapped[datetime | None]
    created_by_user_id: Mapped[UUID]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class GroupMembership(Base):
    """Current access to a group; never a historical reference for plan content."""

    __tablename__ = "group_memberships"
    __table_args__ = {"schema": "groups"}  # noqa: RUF012

    group_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    invited_by_user_id: Mapped[UUID | None]
    joined_at: Mapped[datetime | None]
    left_at: Mapped[datetime | None]
    removed_at: Mapped[datetime | None]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class GroupInvite(Base):
    """A shareable link that adds registered people to a group."""

    __tablename__ = "group_invites"
    __table_args__ = {"schema": "groups"}  # noqa: RUF012

    id: Mapped[UUID] = mapped_column(primary_key=True)
    group_id: Mapped[UUID]
    token_hash: Mapped[bytes] = mapped_column(LargeBinary)
    role: Mapped[str] = mapped_column(Text)
    max_uses: Mapped[int | None]
    use_count: Mapped[int]
    expires_at: Mapped[datetime]
    state: Mapped[str] = mapped_column(Text)
    revoked_at: Mapped[datetime | None]
    created_by_user_id: Mapped[UUID]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
