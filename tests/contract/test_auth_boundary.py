from __future__ import annotations

import httpx


async def test_protected_route_requires_bearer_credential(client: httpx.AsyncClient) -> None:
    response = await client.get("/internal/whoami")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"


async def test_unconfigured_verifier_fails_closed(client: httpx.AsyncClient) -> None:
    response = await client.get("/internal/whoami", headers={"Authorization": "Bearer fake"})

    assert response.status_code == 503
    assert response.json()["code"] == "AUTHENTICATION_UNAVAILABLE"
