"""Small HTTP helpers for API-level tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from beluno.modules.iam.external_identity import IdentityProvider
from beluno.testkit.identity import IdentityProviderStub


@dataclass
class SignedIn:
    user_id: str
    access_token: str
    refresh_token: str | None
    session_id: str
    profile: dict[str, Any]

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


def signed_in_from(payload: dict[str, Any]) -> SignedIn:
    return SignedIn(
        user_id=payload["user"]["id"],
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        session_id=payload["session_id"],
        profile=payload["user"],
    )


async def sign_in(
    api: httpx.AsyncClient,
    provider: IdentityProviderStub,
    *,
    name: str = "Test Member",
    email: str | None = None,
    subject: str | None = None,
    platform: str = "ios",
) -> SignedIn:
    token = provider.id_token(IdentityProvider.GOOGLE, subject=subject, email=email, name=name)
    response = await api.post(
        "/v1/auth/google",
        json={"id_token": token, "device": {"platform": platform, "label": "Test device"}},
    )
    assert response.status_code == 200, response.text
    return signed_in_from(response.json())


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
