"""Push tokens and a person's notification settings (S14)."""

from __future__ import annotations

from datetime import time
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PushTokenRequest(BaseModel):
    """The FCM registration token of this device, for the current session."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=4096, pattern=r"^[\x21-\x7e]+$")
    platform: Literal["ios", "android", "web"]


class NotificationSettingsBody(BaseModel):
    """Categories and quiet hours (in the person's own time zone). Security alerts are
    always sent, quiet hours or not."""

    model_config = ConfigDict(extra="forbid")

    money: bool = Field(description="Expenses and payments that involve you")
    reminders: bool = Field(description="Tasks due and polls closing")
    summaries: bool = Field(description="Daily and weekly summaries")
    news: bool = Field(description="News and offers")
    quiet_hours: bool
    quiet_start: time = Field(description="Local time, e.g. 22:00")
    quiet_end: time = Field(description="Local time, e.g. 07:00")


class NotificationSettingsResponse(NotificationSettingsBody):
    version: int = Field(description="0 until first saved (the defaults apply)")


class PaymentNudgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    participant_id: UUID = Field(description="Who owes you")


class NudgeResponse(BaseModel):
    queued: bool = Field(description="False when already nudged for this today")
