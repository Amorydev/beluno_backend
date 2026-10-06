"""HTTP conventions: optimistic concurrency headers and opaque list cursors."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Annotated
from uuid import UUID

from fastapi import Header, Query, Response

from beluno.contracts.errors import precondition_required, validation_error

ETAG_PATTERN = re.compile(r'^(?:W/)?"(\d{1,9})"$')
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


def parse_if_match(value: str | None) -> int:
    """``If-Match: "<version>"`` is required on every update of a versioned resource."""

    if value is None:
        raise precondition_required()
    match = ETAG_PATTERN.match(value.strip())
    if match is None:
        raise validation_error('If-Match must be a quoted entity version such as "3"')
    return int(match.group(1))


def set_etag(response: Response, version: int) -> None:
    response.headers["ETag"] = f'"{version}"'


def encode_cursor(last_id: UUID) -> str:
    return base64.urlsafe_b64encode(last_id.bytes).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str | None) -> UUID | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        return UUID(bytes=raw)
    except (binascii.Error, ValueError) as error:
        raise validation_error("cursor is invalid") from error


IfMatch = Annotated[str | None, Header(alias="If-Match")]
CursorParam = Annotated[str | None, Query(max_length=64)]
LimitParam = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
