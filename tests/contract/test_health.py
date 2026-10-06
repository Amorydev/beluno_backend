from __future__ import annotations

import httpx


async def test_liveness_is_available_without_dependencies(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"]


async def test_readiness_reports_dependency_failure(degraded_client: httpx.AsyncClient) -> None:
    response = await degraded_client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded"}
