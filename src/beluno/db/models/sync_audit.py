"""Sync kernel records: per-scope heads and idempotent operation outcomes.

Change rows are written only through ``sync_audit.append_changes`` and read only
through ``sync_audit.read_changes``; they have no mapping here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import LargeBinary, SmallInteger, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from beluno.db.models.base import Base


class ScopeHead(Base):
    """The latest sequence, compaction floor, and generation of one sync scope."""

    __tablename__ = "scope_heads"
    __table_args__ = {"schema": "sync_audit"}  # noqa: RUF012

    scope_type: Mapped[str] = mapped_column(Text, primary_key=True)
    scope_id: Mapped[UUID] = mapped_column(primary_key=True)
    last_seq: Mapped[int]
    floor_seq: Mapped[int]
    generation: Mapped[int]
    updated_at: Mapped[datetime]


class OperationRecord(Base):
    """The canonical outcome of a command that carried an idempotency key."""

    __tablename__ = "operations"
    __table_args__ = {"schema": "sync_audit"}  # noqa: RUF012

    id: Mapped[UUID] = mapped_column(primary_key=True)
    actor_user_id: Mapped[UUID]
    command: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text)
    request_hash: Mapped[bytes] = mapped_column(LargeBinary)
    source: Mapped[str] = mapped_column(Text)
    session_id: Mapped[UUID | None]
    device_id: Mapped[str | None] = mapped_column(Text)
    client_created_at: Mapped[datetime | None]
    response_status: Mapped[int] = mapped_column(SmallInteger)
    response_body: Mapped[Any] = mapped_column(JSONB)
    response_etag: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]
