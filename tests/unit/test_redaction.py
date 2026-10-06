from __future__ import annotations

from beluno.observability.redaction import redact


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
