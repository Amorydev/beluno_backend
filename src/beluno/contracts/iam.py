"""Identity, session, and profile contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from beluno.contracts.common import DisplayName, LocaleTag, TimezoneName

EMAIL_PATTERN = r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$"


class DeviceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_device_id: str | None = Field(default=None, min_length=1, max_length=64)
    label: str | None = Field(default=None, min_length=1, max_length=80)
    platform: Literal["ios", "android", "web", "other"] | None = None
    app_version: str | None = Field(default=None, min_length=1, max_length=32)


class ExternalSignInRequest(BaseModel):
    """Google/Apple ID token. With a guest bearer token this upgrades or claims the guest;
    with a registered bearer token it re-authenticates (step-up) the current session."""

    model_config = ConfigDict(extra="forbid")

    id_token: str = Field(min_length=20, max_length=8_192)
    nonce: str | None = Field(default=None, min_length=8, max_length=256)
    display_name: DisplayName | None = None
    device: DeviceRequest | None = None
    merge_guest_participations: bool = False


class EmailChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=320, pattern=EMAIL_PATTERN)


class EmailChallengeResponse(BaseModel):
    challenge_id: UUID
    expires_at: datetime


class EmailVerifyRequest(BaseModel):
    """Either ``challenge_id`` + ``code`` from the email, or the magic-link ``link_token``."""

    model_config = ConfigDict(extra="forbid")

    challenge_id: UUID | None = None
    code: str | None = Field(default=None, pattern=r"^[0-9]{6}$")
    link_token: str | None = Field(default=None, min_length=20, max_length=128)
    display_name: DisplayName | None = None
    device: DeviceRequest | None = None
    merge_guest_participations: bool = False

    @model_validator(mode="after")
    def exactly_one_proof(self) -> Self:
        code_parts = (self.challenge_id is not None, self.code is not None)
        valid = not any(code_parts) if self.link_token is not None else all(code_parts)
        if not valid:
            raise ValueError("provide challenge_id with code, or link_token")
        return self


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=20, max_length=256)


class UserProfileResponse(BaseModel):
    id: UUID
    kind: Literal["registered", "guest"]
    display_name: str
    email: str | None
    locale: str | None
    timezone: str | None
    version: int


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_at: datetime
    refresh_token: str | None = Field(
        description="Present when a new session starts or rotates; absent after step-up."
    )
    session_id: UUID
    user: UserProfileResponse


class ProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: DisplayName | None = None
    locale: LocaleTag | None = None
    timezone: TimezoneName | None = None


class SessionResponse(BaseModel):
    id: UUID
    auth_method: str
    platform: str | None
    device_label: str | None
    app_version: str | None
    created_at: datetime
    last_seen_at: datetime
    authenticated_at: datetime
    current: bool


class JwksResponse(BaseModel):
    keys: list[dict[str, str]]
