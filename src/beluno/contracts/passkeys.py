"""Passkeys (WebAuthn): add one, sign in or step up with one, and manage them."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from beluno.contracts.common import clean_text
from beluno.contracts.iam import DeviceRequest

Credential = dict[str, Any]
PasskeyLabel = Annotated[
    str,
    StringConstraints(max_length=400),
    AfterValidator(clean_text),
    StringConstraints(min_length=1, max_length=64),
]


class PasskeyOptionsResponse(BaseModel):
    """Pass ``public_key`` to the platform's WebAuthn API, then send the result back
    with ``challenge_id`` within five minutes."""

    challenge_id: UUID
    public_key: dict[str, Any]


class PasskeyRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: UUID
    credential: Credential = Field(description="The RegistrationResponseJSON from the device")
    label: PasskeyLabel = Field(description="e.g. the device's name")


class PasskeySignInRequest(BaseModel):
    """Signed out: start a session. Signed in: step up the current session. As a guest:
    claim the account the passkey belongs to (as with Google/Apple)."""

    model_config = ConfigDict(extra="forbid")

    challenge_id: UUID
    credential: Credential = Field(description="The AuthenticationResponseJSON from the device")
    device: DeviceRequest | None = None
    merge_guest_participations: bool = False


class PasskeyRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: PasskeyLabel


class PasskeyResponse(BaseModel):
    id: UUID
    label: str
    backed_up: bool = Field(description="Synced by the platform (iCloud Keychain, Google)")
    transports: list[str]
    created_at: datetime
    last_used_at: datetime | None
