"""Support: problem reports people send from the app."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from beluno.db.models.base import Base

SCHEMA = {"schema": "analytics_ops"}


class ProblemReport(Base):
    __tablename__ = "problem_reports"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID]
    category: Mapped[str] = mapped_column(Text)
    plan_id: Mapped[UUID | None]
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[UUID | None]
    message: Mapped[str] = mapped_column(Text)
    diagnostic_code: Mapped[str | None] = mapped_column(Text)
    diagnostics: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime]
