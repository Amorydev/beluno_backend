"""Uploaded files of a plan: receipts on expenses and trip covers."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

MediaKind = Literal["receipt", "cover"]
MediaState = Literal["awaiting_upload", "scanning", "ready", "rejected"]
DeclaredType = Literal["image/jpeg", "image/png", "image/webp", "image/heic", "application/pdf"]


class MediaCreateRequest(BaseModel):
    """Record a file before uploading it (works offline through sync push)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    kind: MediaKind
    content_type: DeclaredType = Field(description="PDF only for receipts")
    size_bytes: int = Field(gt=0, le=100 * 1024 * 1024)
    expense_id: UUID | None = Field(default=None, description="Receipts: the expense")


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
    uploaded_by_user_id: UUID
    version: int
    created_at: datetime
    updated_at: datetime


class SignedUrlResponse(BaseModel):
    """A presigned storage URL; upload with PUT and the declared Content-Type and size,
    download with GET. It stops working at ``expires_at``."""

    url: str
    expires_at: datetime
