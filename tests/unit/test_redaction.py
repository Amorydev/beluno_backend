from __future__ import annotations

from beluno.observability.redaction import redact
from beluno.observability.setup import redact_sentry_event


def test_redacts_sensitive_keys_recursively() -> None:
    payload = {
        "authorization": "Bearer secret",
        "nested": {"confirmation_code": "ABC-123", "safe": "visible"},
        "items": [{"token": "secret"}],
    }

    assert redact(payload) == {
        "authorization": "[REDACTED]",
        "nested": {"confirmation_code": "[REDACTED]", "safe": "visible"},
        "items": [{"token": "[REDACTED]"}],
    }


def test_sentry_events_lose_credentials_and_contact_details() -> None:
    event = {
        "request": {
            "headers": {"Authorization": "Bearer eyJsecret", "Cookie": "sid=1"},
            "data": {"refresh_token": "rt-secret", "email": "a@example.com"},
        },
        "extra": {"intended_email": "b@example.com", "event": "invite_unavailable"},
    }

    redacted = redact_sentry_event(event, {})  # type: ignore[arg-type]

    flat = str(redacted)
    for secret in ("eyJsecret", "sid=1", "rt-secret", "a@example.com", "b@example.com"):
        assert secret not in flat
    assert redacted is not None and redacted["extra"]["event"] == "invite_unavailable"
