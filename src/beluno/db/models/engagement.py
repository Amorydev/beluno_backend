"""Engagement: push tokens, notification settings, and the notification outbox."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any
from uuid import UUID

from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from beluno.db.models.base import Base

SCHEMA = {"schema": "engagement"}


class PushToken(Base):
    __tablename__ = "push_tokens"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID]
    session_id: Mapped[UUID]
    token: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class NotificationSettings(Base):
    __tablename__ = "notification_settings"
    __table_args__ = SCHEMA

    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    money: Mapped[bool]
    reminders: Mapped[bool]
    summaries: Mapped[bool]
    news: Mapped[bool]
    quiet_hours: Mapped[bool]
    quiet_start: Mapped[time]
    quiet_end: Mapped[time]
    version: Mapped[int]
    updated_at: Mapped[datetime]


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID]
    category: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    plan_id: Mapped[UUID | None]
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[UUID | None]
    args: Mapped[dict[str, Any]] = mapped_column(JSONB)
    timezone: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int]
    deliver_after: Mapped[datetime]
    expires_at: Mapped[datetime | None]
    created_at: Mapped[datetime]
    sent_at: Mapped[datetime | None]
