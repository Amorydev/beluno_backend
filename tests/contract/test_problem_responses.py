from __future__ import annotations

import logging
import secrets

import httpx
import pytest

from beluno.api.main import create_app
from beluno.config import Settings
from beluno.testkit.environment import generate_signing_keys_json


async def test_not_found_uses_problem_contract(client: httpx.AsyncClient) -> None:
    response = await client.get("/missing-resource")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "NOT_FOUND"
    assert response.json()["request_id"] == response.headers["x-request-id"]


async def test_validation_failure_uses_problem_contract() -> None:
    app = create_app(settings=Settings())

    @app.get("/test-only-value")
    async def test_only_value(value: int) -> dict[str, int]:
        return {"value": value}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/test-only-value?value=not-an-integer")

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "VALIDATION_FAILED"


async def test_invalid_bearer_token_uses_problem_contract() -> None:
    app = create_app(
        settings=Settings(
            _env_file=None,  # type: ignore[call-arg]
            auth_signing_keys=generate_signing_keys_json(),
            token_hash_key=secrets.token_urlsafe(40),
        ),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/internal/whoami", headers={"Authorization": "Bearer invalid"})

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"
    assert response.json()["request_id"] == response.headers["x-request-id"]


async def test_oversized_body_is_rejected_before_routing() -> None:
    app = create_app(settings=Settings(api_max_request_bytes=1_024))

    @app.post("/test-only-payload")
    async def test_only_payload(payload: dict[str, str]) -> dict[str, str]:
        return payload

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/test-only-payload", json={"value": "x" * 2_000})

    assert response.status_code == 413
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "REQUEST_TOO_LARGE"
    assert response.json()["request_id"] == response.headers["x-request-id"]


async def test_unexpected_errors_use_problem_contract_and_log_their_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(settings=Settings())

    @app.get("/test-only-crash")
    async def test_only_crash() -> None:
        raise RuntimeError("amount 12345 for secret dinner")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    with caplog.at_level(logging.ERROR):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/test-only-crash")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "INTERNAL_ERROR"
    assert "secret dinner" not in response.text
    ours = [record for record in caplog.records if record.name == "beluno.api"]
    assert [getattr(record, "error", None) for record in ours] == ["RuntimeError"]
    assert all("secret dinner" not in record.getMessage() for record in ours)
