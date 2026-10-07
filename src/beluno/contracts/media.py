"""Uploaded files of a plan: receipts on expenses and trip covers."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from beluno.contracts.common import LongText

MediaKind = Literal["receipt", "cover", "memory"]
MediaState = Literal["awaiting_upload", "scanning", "ready", "rejected"]
DeclaredType = Literal["image/jpeg", "image/png", "image/webp", "image/heic", "application/pdf"]


class MemoryDetails(BaseModel):
    """A memory's caption, local day and time (the server strips EXIF), and place."""

    model_config = ConfigDict(extra="forbid")

    caption: LongText | None = Field(default=None, max_length=280)
    day: date | None = None
    taken_time: time | None = Field(default=None, description="Local time; needs day")
    place_id: UUID | None = Field(default=None, description="A saved place of the trip")

    @model_validator(mode="after")
    def _time_needs_day(self) -> MemoryDetails:
        if self.taken_time is not None and (self.day is None or self.taken_time.tzinfo):
            raise ValueError("taken_time is a local time on day")
        return self


class MediaCreateRequest(BaseModel):
    """Record a file before uploading it (works offline through sync push)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    kind: MediaKind
    content_type: DeclaredType = Field(description="PDF only for receipts")
    size_bytes: int = Field(gt=0, le=100 * 1024 * 1024)
    expense_id: UUID | None = Field(default=None, description="Receipts: the expense")
    memory: MemoryDetails | None = Field(default=None, description="Memories: the details")


class HighlightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    in_recap: bool


class MediaResponse(BaseModel):
    id: UUID
    plan_id: UUID
    kind: MediaKind
    state: MediaState
    rejection: Literal["type", "size", "malware", "unreadable", "missing"] | None
    content_type: str | None = Field(description="Once ready: the stored file's type")
    size_bytes: int | None
    width: int | None
    height: int | None
    expense_id: UUID | None
    caption: str | None
    day: date | None
    taken_time: time | None
    place_id: UUID | None
    in_recap: bool = Field(description="An organiser's pick for the trip recap")
    uploaded_by_user_id: UUID
    version: int
    created_at: datetime
    updated_at: datetime


class SignedUrlResponse(BaseModel):
    """A presigned storage URL; upload with PUT and the declared Content-Type and size,
    download with GET. It stops working at ``expires_at``."""

    url: str
    expires_at: datetime
