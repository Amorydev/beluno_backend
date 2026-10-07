"""Trip planning: polls, their options, electorate, votes, results, and outcomes."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ARRAY, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from beluno.db.models.base import Base

SCHEMA = {"schema": "decisions"}


class Poll(Base):
    __tablename__ = "polls"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    kind: Mapped[str] = mapped_column(Text)
    question: Mapped[str] = mapped_column(Text)
    deadline_at: Mapped[datetime | None]
    quorum: Mapped[int | None]
    allow_vote_change: Mapped[bool]
    status: Mapped[str] = mapped_column(Text)
    created_by_user_id: Mapped[UUID]
    closed_at: Mapped[datetime | None]
    closed_by_user_id: Mapped[UUID | None]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    deleted_at: Mapped[datetime | None]


class PollOption(Base):
    __tablename__ = "poll_options"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    poll_id: Mapped[UUID]
    plan_id: Mapped[UUID]
    label: Mapped[str] = mapped_column(Text)
    place_id: Mapped[UUID | None]
    answer: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int]


class PollElector(Base):
    """A participant who was active when the poll opened, so may vote on it."""

    __tablename__ = "poll_electorate"
    __table_args__ = SCHEMA

    poll_id: Mapped[UUID] = mapped_column(primary_key=True)
    participant_id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]


class PollVote(Base):
    __tablename__ = "poll_votes"
    __table_args__ = SCHEMA

    poll_id: Mapped[UUID] = mapped_column(primary_key=True)
    participant_id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    option_id: Mapped[UUID]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class PollResult(Base):
    """Written once, by the database, when the poll closes."""

    __tablename__ = "poll_results"
    __table_args__ = SCHEMA

    poll_id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    version: Mapped[int]
    outcome: Mapped[str] = mapped_column(Text)
    winner_option_id: Mapped[UUID | None]
    tied_option_ids: Mapped[list[UUID]] = mapped_column(ARRAY(Uuid(as_uuid=True)))
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB)
    eligible: Mapped[int]
    voted: Mapped[int]
    closed_at: Mapped[datetime]
    closed_by_user_id: Mapped[UUID | None]


class PollOutcome(Base):
    """What was done with a result (one per poll and action)."""

    __tablename__ = "poll_outcomes"
    __table_args__ = SCHEMA

    poll_id: Mapped[UUID] = mapped_column(primary_key=True)
    action: Mapped[str] = mapped_column(Text, primary_key=True)
    plan_id: Mapped[UUID]
    result_version: Mapped[int]
    option_id: Mapped[UUID]
    created_entity_id: Mapped[UUID]
    created_by_user_id: Mapped[UUID]
    created_at: Mapped[datetime]
