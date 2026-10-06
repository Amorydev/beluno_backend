"""Declarative base for typed queries.

Reviewed Alembic SQL owns the DDL. Models declare no server defaults: every value,
including IDs and timestamps, is set by the application so inserts never need
``RETURNING`` (which RLS would evaluate against rows the actor cannot read yet).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Uuid
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy class-level configuration
        datetime: DateTime(timezone=True),
        UUID: Uuid(as_uuid=True),
    }
