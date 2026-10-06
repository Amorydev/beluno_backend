"""Redact sensitive fields before data reaches logs or telemetry."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "link_token",
        "code",
        "nonce",
        "intended_email",
        "idempotency_key",
        "database_url",
        "password",
        "secret",
        "signed_url",
        "booking_code",
        "confirmation_code",
        "email",
        "phone",
        "contact",
        "address",
        "location",
        "latitude",
        "longitude",
        "finance_description",
        "receipt",
    }
)


def redact(value: Any) -> Any:
    """Return a recursively redacted telemetry-safe representation."""

    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if str(key).lower() in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value
