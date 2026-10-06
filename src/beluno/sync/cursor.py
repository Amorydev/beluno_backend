"""Opaque, integrity-protected pull cursors.

A cursor binds the user, the scope, the scope generation, the access level it
was issued under, and the position: either a change sequence (plus the high
watermark while a multi-page pull is in flight) or snapshot progress. The
payload is signed with an HMAC derived from ``BELUNO_TOKEN_HASH_KEY`` so a
client cannot forge, edit, or move a cursor to another scope or account.
"""

from __future__ import annotations

import base64
import hmac
import json
from dataclasses import dataclass, replace
from typing import Literal
from uuid import UUID

from beluno.sync.scopes import AccessLevel, ScopeKey
from beluno.token_hashing import TokenHasher

CURSOR_VERSION = 1
CURSOR_PURPOSE = "sync_cursor"
SIGNATURE_BYTES = 16


class CursorError(ValueError):
    """The cursor is not one this server issued to this caller; resync is required."""


@dataclass(frozen=True)
class Cursor:
    user_id: UUID
    scope: ScopeKey
    generation: int
    level: AccessLevel
    mode: Literal["changes", "snapshot"]
    # changes: the last applied sequence; snapshot: the head captured when it started.
    seq: int
    # changes: the watermark of an in-flight multi-page pull.
    watermark: int | None = None
    # snapshot progress: entity-type phase index and the last emitted ID in it.
    phase: int = 0
    after: UUID | None = None

    @classmethod
    def snapshot_start(
        cls, user_id: UUID, scope: ScopeKey, generation: int, level: AccessLevel, head: int
    ) -> Cursor:
        return cls(user_id, scope, generation, level, "snapshot", head)

    def continue_snapshot(self, phase: int, after: UUID | None) -> Cursor:
        return replace(self, mode="snapshot", phase=phase, after=after)

    def at_changes(self, seq: int, watermark: int | None) -> Cursor:
        return replace(self, mode="changes", seq=seq, watermark=watermark, phase=0, after=None)


class CursorCodec:
    def __init__(self, hasher: TokenHasher) -> None:
        self._hasher = hasher

    def encode(self, cursor: Cursor) -> str:
        payload = {
            "v": CURSOR_VERSION,
            "u": str(cursor.user_id),
            "s": str(cursor.scope),
            "g": cursor.generation,
            "l": cursor.level.value,
            "m": cursor.mode,
            "q": cursor.seq,
            "w": cursor.watermark,
            "p": cursor.phase,
            "a": str(cursor.after) if cursor.after else None,
        }
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return base64.urlsafe_b64encode(body + self._sign(body)).rstrip(b"=").decode("ascii")

    def decode(self, token: str, *, user_id: UUID) -> Cursor:
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        except ValueError as error:
            raise CursorError("cursor is not decodable") from error
        body, signature = raw[:-SIGNATURE_BYTES], raw[-SIGNATURE_BYTES:]
        try:
            expected = self._sign(body)
        except UnicodeDecodeError as error:
            raise CursorError("cursor body is not text") from error
        if len(body) == 0 or not hmac.compare_digest(signature, expected):
            raise CursorError("cursor signature is invalid")
        try:
            payload = json.loads(body)
            if payload["v"] != CURSOR_VERSION or UUID(payload["u"]) != user_id:
                raise CursorError("cursor belongs to another version or account")
            return Cursor(
                user_id=user_id,
                scope=ScopeKey.parse(payload["s"]),
                generation=int(payload["g"]),
                level=AccessLevel(payload["l"]),
                mode="snapshot" if payload["m"] == "snapshot" else "changes",
                seq=int(payload["q"]),
                watermark=None if payload["w"] is None else int(payload["w"]),
                phase=int(payload["p"]),
                after=UUID(payload["a"]) if payload["a"] else None,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise CursorError("cursor payload is invalid") from error

    def _sign(self, body: bytes) -> bytes:
        return self._hasher.digest(CURSOR_PURPOSE, body.decode("utf-8"))[:SIGNATURE_BYTES]


def encode_directory_cursor(last_scope: ScopeKey) -> str:
    return base64.urlsafe_b64encode(str(last_scope).encode("ascii")).rstrip(b"=").decode("ascii")


def decode_directory_cursor(token: str) -> ScopeKey:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("ascii")
    except (ValueError, UnicodeDecodeError) as error:
        raise CursorError("directory cursor is invalid") from error
    return ScopeKey.parse(raw)
