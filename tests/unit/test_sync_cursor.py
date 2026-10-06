"""Cursor integrity: round trips, tampering, and account binding."""

from __future__ import annotations

from uuid import uuid4

import pytest

from beluno.modules.sync_audit.recorder import ChangeScope
from beluno.sync.cursor import (
    Cursor,
    CursorCodec,
    CursorError,
    decode_directory_cursor,
    encode_directory_cursor,
)
from beluno.sync.scopes import AccessLevel, ScopeKey
from beluno.token_hashing import TokenHasher

HASHER = TokenHasher("unit-test-cursor-key-with-enough-length")


def test_cursor_round_trips_every_field() -> None:
    codec = CursorCodec(HASHER)
    user_id = uuid4()
    scope = ScopeKey(ChangeScope.PLAN, uuid4())
    for cursor in (
        Cursor(user_id, scope, 3, AccessLevel.MANAGER, "changes", 41, watermark=50),
        Cursor(user_id, scope, 1, AccessLevel.READER, "changes", 0),
        Cursor.snapshot_start(user_id, scope, 2, AccessLevel.MEMBER, 17).continue_snapshot(
            2, uuid4()
        ),
    ):
        token = codec.encode(cursor)
        assert "=" not in token and len(token) < 512
        assert codec.decode(token, user_id=user_id) == cursor


def test_tampered_and_foreign_cursors_are_rejected() -> None:
    codec = CursorCodec(HASHER)
    user_id = uuid4()
    cursor = Cursor(
        user_id, ScopeKey(ChangeScope.GROUP, uuid4()), 1, AccessLevel.MEMBER, "changes", 5
    )
    token = codec.encode(cursor)

    with pytest.raises(CursorError):
        codec.decode(token, user_id=uuid4())
    with pytest.raises(CursorError):
        codec.decode(token[:-2] + ("AA" if token[-2:] != "AA" else "BB"), user_id=user_id)
    with pytest.raises(CursorError):
        codec.decode("not-a-cursor", user_id=user_id)
    with pytest.raises(CursorError):
        codec.decode("", user_id=user_id)
    with pytest.raises(CursorError):
        CursorCodec(TokenHasher("another-key-that-is-also-long-enough")).decode(
            token, user_id=user_id
        )


def test_directory_cursor_round_trip_and_validation() -> None:
    scope = ScopeKey(ChangeScope.PLAN, uuid4())
    assert decode_directory_cursor(encode_directory_cursor(scope)) == scope
    with pytest.raises(Exception, match="invalid"):
        decode_directory_cursor("bm90LWEtc2NvcGU")


def test_binary_and_truncated_cursor_bodies_are_rejected() -> None:
    import base64

    codec = CursorCodec(HASHER)
    for raw in (b"\xff" * 40, b"\xff\xfe" + b"\x00" * 16, b"\x80", b"A" * 16):
        token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        with pytest.raises(CursorError):
            codec.decode(token, user_id=uuid4())
