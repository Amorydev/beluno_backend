"""HTTP conventions: optimistic concurrency headers, idempotency, and list cursors."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Annotated, TypeVar
from uuid import UUID

from fastapi import Header, Query, Response, status
from pydantic import BaseModel

from beluno.contracts.errors import precondition_required, validation_error
from beluno.sync.commands import CommandCall, CommandResult

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
IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]{1,128}$",
        description=(
            "Optional client-generated key; a repeat with the same key and body returns "
            "the stored outcome and sets Idempotency-Replayed: true."
        ),
    ),
]
CursorParam = Annotated[str | None, Query(max_length=64)]
LimitParam = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]

BodyT = TypeVar("BodyT", bound=BaseModel)


def command_call(
    idempotency_key: str | None = None,
    *,
    if_match: str | None = None,
    **target: UUID,
) -> CommandCall:
    """Build a command invocation from REST path IDs and headers."""

    return CommandCall(
        target=target,
        expected_version=parse_if_match(if_match) if if_match is not None else None,
        idempotency_key=idempotency_key,
    )


def finish(response: Response, result: CommandResult[BodyT]) -> BodyT:
    """Apply the command's headers to the response and return its body."""

    if result.etag_version is not None:
        set_etag(response, result.etag_version)
    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return result.body


def finish_empty(result: CommandResult[None]) -> Response:
    headers = {"Idempotency-Replayed": "true"} if result.replayed else None
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=headers)
