"""Spans and logs carry routes, statuses, and event names, never secrets or personal text."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from beluno.api.main import create_app
from beluno.config import Settings
from beluno.db.session import Database
from beluno.observability.setup import instrument_api
from beluno.testkit.api_client import sign_in, signed_in_from
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import equal_expense
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration

EMAIL = "linh.private@example.com"
DESCRIPTION = "Dinner at Linh's flat, 12 Hang Bac"
NOTES = "card ending 4471"
BOOKING_CODE = "PNR-ZX81QK"


async def test_spans_and_logs_hold_no_secrets_or_personal_text(
    live_settings: Settings,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="beluno")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    database = Database(live_settings)
    app = create_app(
        settings=live_settings,
        database=database,
        identity_verifier=identity_provider.verifier(live_settings),
    )
    instrument_api(app, tracer_provider=provider)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as api:
        linh = await sign_in(api, identity_provider, name="Linh Secretname")
        await api.post("/v1/auth/email/challenges", json={"email": EMAIL})
        plan = (
            await api.post(
                "/v1/plans",
                json={"type": "trip", "title": "Hanoi", "base_currency": "VND"},
                headers=linh.headers,
            )
        ).json()
        participant = (
            await api.get(f"/v1/plans/{plan['id']}/participants", headers=linh.headers)
        ).json()[0]["id"]
        added = await api.post(
            f"/v1/plans/{plan['id']}/expenses",
            json=equal_expense(
                100_000,
                participant,
                [participant],
                currency="VND",
                description=DESCRIPTION,
                notes=NOTES,
            ),
            headers=linh.headers,
        )
        assert added.status_code == 201, added.text
        booked = await api.post(
            f"/v1/plans/{plan['id']}/bookings",
            json={
                "kind": "flight",
                "title": "VN 301",
                "secrets": {"confirmation_code": BOOKING_CODE},
            },
            headers=linh.headers,
        )
        assert booked.status_code == 201, booked.text
        revealed = await api.post(
            f"/v1/plans/{plan['id']}/bookings/{booked.json()['id']}/reveal", headers=linh.headers
        )
        assert revealed.json()["confirmation_code"] == BOOKING_CODE
        # Security events: an unknown invite link and a replayed refresh token.
        await api.post("/v1/invites/redeem", json={"token": "x" * 43}, headers=linh.headers)
        rotated = await api.post("/v1/auth/refresh", json={"refresh_token": linh.refresh_token})
        fresh = signed_in_from(rotated.json())
        admin.execute(
            "UPDATE iam.refresh_tokens SET consumed_at = consumed_at - interval '1 hour' "
            "WHERE consumed_at IS NOT NULL"
        )
        await api.post("/v1/auth/refresh", json={"refresh_token": linh.refresh_token})
    await database.close()

    spans = exporter.get_finished_spans()
    assert any(span.attributes and "/expenses" in str(span.attributes) for span in spans)
    traced = json.dumps(
        [{"name": span.name, **dict(span.attributes or {})} for span in spans], default=str
    )
    records = [record for record in caplog.records if record.name.startswith("beluno")]
    assert {getattr(record, "event", None) for record in records} >= {
        "invite_unavailable",
        "refresh_reuse",
    }
    logged = json.dumps([record.__dict__ for record in records], default=str)
    secrets = [
        linh.access_token,
        linh.refresh_token,
        fresh.refresh_token,
        "x" * 43,
        EMAIL,
        DESCRIPTION,
        NOTES,
        BOOKING_CODE,
        "Secretname",
    ]
    for secret in secrets:
        assert secret
        assert secret not in traced, secret
        assert secret not in logged, secret
