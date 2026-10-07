"""Worker jobs run under the worker database role against real PostgreSQL."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from beluno.auth import AccessTokenCodec
from beluno.config import Settings
from beluno.db.session import Database
from beluno.modules.context import Runtime
from beluno.modules.iam.external_identity import ExternalIdentityVerifier
from beluno.testkit.api_client import sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.environment import RecordingEmailSender
from beluno.testkit.identity import IdentityProviderStub
from beluno.token_hashing import TokenHasher
from beluno.worker import tasks

pytestmark = pytest.mark.integration


@pytest.fixture
async def worker_runtime(
    live_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Runtime, RecordingEmailSender]]:
    database = Database.for_worker(live_settings)
    runtime = Runtime(
        settings=live_settings,
        database=database,
        tokens=AccessTokenCodec(live_settings),
        hasher=TokenHasher.from_settings(live_settings),
        identity_verifier=ExternalIdentityVerifier(live_settings),
    )
    sender = RecordingEmailSender()
    monkeypatch.setattr(tasks, "get_worker_runtime", lambda: runtime)
    monkeypatch.setattr(tasks, "get_email_sender", lambda: sender)
    yield runtime, sender
    await database.close()


async def test_email_job_delivers_once_and_rejects_unknown_payloads(
    api: httpx.AsyncClient,
    worker_runtime: tuple[Runtime, RecordingEmailSender],
    admin: AdminDatabase,
) -> None:
    _, sender = worker_runtime
    started = await api.post("/v1/auth/email/challenges", json={"email": "job@example.com"})
    challenge_id = started.json()["challenge_id"]

    assert await tasks.deliver_email_challenge.func(challenge_id) is True
    assert await tasks.deliver_email_challenge.func(challenge_id) is False
    assert len(sender.messages) == 1
    assert sender.messages[0].to == "job@example.com"
    assert "https://app.beluno.test/auth/email#token=" in sender.messages[0].text_body
    assert admin.scalar("SELECT delivery_state FROM iam.email_challenges") == "sent"
    with pytest.raises(ValueError, match="payload version"):
        await tasks.deliver_email_challenge.func(challenge_id, payload_version=2)


async def test_purge_job_removes_only_expired_auth_records(
    api: httpx.AsyncClient,
    worker_runtime: tuple[Runtime, RecordingEmailSender],
    admin: AdminDatabase,
) -> None:
    for email in ("old@example.com", "new@example.com"):
        await api.post("/v1/auth/email/challenges", json={"email": email})
    admin.execute(
        "UPDATE iam.email_challenges SET expires_at = now() - interval '2 days' "
        "WHERE email = 'old@example.com'"
    )
    admin.execute("UPDATE iam.rate_limit_counters SET window_start = now() - interval '3 days'")

    removed = await tasks.purge_expired_auth.func(0)

    assert removed >= 2
    assert admin.fetch("SELECT email FROM iam.email_challenges") == [("new@example.com",)]
    assert admin.scalar("SELECT count(*) FROM iam.rate_limit_counters") == 0


async def test_purge_job_forgets_sessions_that_ended_a_month_ago(
    api: httpx.AsyncClient,
    worker_runtime: tuple[Runtime, RecordingEmailSender],
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
) -> None:
    old, new, live = [
        await sign_in(api, identity_provider, name=name) for name in ("Old", "New", "Live")
    ]
    for person, days in ((old, 31), (new, 10)):
        admin.execute(
            "UPDATE iam.sessions SET revoked_at = now() - make_interval(days => %s), "
            "revoked_reason = 'logout' WHERE id = %s",
            days,
            person.session_id,
        )
    assert admin.scalar(
        "SELECT count(*) FROM iam.refresh_tokens WHERE session_id = %s", old.session_id
    )

    await tasks.purge_expired_auth.func(0)

    assert {row[0] for row in admin.fetch("SELECT id::text FROM iam.sessions")} == {
        new.session_id,
        live.session_id,
    }
    assert (
        admin.scalar(
            "SELECT count(*) FROM iam.refresh_tokens WHERE session_id = %s", old.session_id
        )
        == 0
    )
