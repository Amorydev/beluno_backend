"""RFC 9562 UUIDv7 identifiers: time-ordered and safe for clients to generate."""

from __future__ import annotations

import secrets
import time
from uuid import UUID


def new_id(timestamp_ms: int | None = None) -> UUID:
    """Return a UUIDv7 with a 48-bit Unix-millisecond prefix and 74 random bits."""

    milliseconds = time.time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms
    value = (milliseconds & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= secrets.randbits(12) << 64
    value |= 0b10 << 62
    value |= secrets.randbits(62)
    return UUID(int=value)
