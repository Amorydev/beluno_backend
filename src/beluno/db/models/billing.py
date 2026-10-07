"""Billing: purchases verified with the App Store and Google Play."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from beluno.db.models.base import Base


class Purchase(Base):
    __tablename__ = "purchases"
    __table_args__ = {"schema": "billing"}  # noqa: RUF012

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID]
    store: Mapped[str] = mapped_column(Text)
    product: Mapped[str] = mapped_column(Text)
    product_id: Mapped[str] = mapped_column(Text)
    original_id: Mapped[str] = mapped_column(Text)
    plan_id: Mapped[UUID | None]
    environment: Mapped[str] = mapped_column(Text)
    purchased_at: Mapped[datetime]
    expires_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    superseded_at: Mapped[datetime | None]
    acknowledged_at: Mapped[datetime | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
