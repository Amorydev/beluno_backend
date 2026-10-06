from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from fastapi import HTTPException

from beluno.auth import AccessTokenCodec, load_signing_keys
from beluno.config import Settings
from beluno.testkit.environment import generate_signing_keys_json


def codec_with(keys_json: str) -> AccessTokenCodec:
    return AccessTokenCodec(
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            auth_signing_keys=keys_json,
            token_hash_key=secrets.token_urlsafe(40),
        )
    )


def test_issued_token_round_trips_with_session_claims() -> None:
    codec = codec_with(generate_signing_keys_json())
    user_id, session_id = uuid4(), uuid4()
    authenticated_at = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=3)

    issued = codec.issue(
        user_id=user_id,
        session_id=session_id,
        authenticated_at=authenticated_at,
        is_guest=True,
        now=datetime.now(UTC),
    )
    actor = codec.verify(issued.token)

    assert actor.user_id == user_id
    assert actor.session_id == session_id
    assert actor.authenticated_at == authenticated_at
    assert actor.is_guest is True


def test_rotated_out_signing_key_still_verifies_until_removed() -> None:
    old_keys = generate_signing_keys_json("2026-09")
    new_keys = generate_signing_keys_json("2026-10")
    old_token = codec_with(old_keys).issue(
        user_id=uuid4(),
        session_id=uuid4(),
        authenticated_at=datetime.now(UTC),
        is_guest=False,
        now=datetime.now(UTC),
    )
    rotated = json.dumps(json.loads(new_keys) + json.loads(old_keys))

    assert codec_with(rotated).verify(old_token.token).is_guest is False
    with pytest.raises(HTTPException) as captured:
        codec_with(new_keys).verify(old_token.token)
    assert captured.value.status_code == 401


def test_expired_and_tampered_tokens_are_rejected() -> None:
    codec = codec_with(generate_signing_keys_json())
    expired = codec.issue(
        user_id=uuid4(),
        session_id=uuid4(),
        authenticated_at=datetime.now(UTC) - timedelta(hours=2),
        is_guest=False,
        now=datetime.now(UTC) - timedelta(hours=2),
    )
    with pytest.raises(HTTPException) as captured:
        codec.verify(expired.token)
    assert captured.value.status_code == 401

    unsigned = jwt.encode({"sub": str(uuid4())}, "secret", algorithm="HS256")
    with pytest.raises(HTTPException):
        codec.verify(unsigned)


def test_unconfigured_codec_fails_closed() -> None:
    codec = AccessTokenCodec(Settings(_env_file=None))  # type: ignore[call-arg]
    assert codec.configured is False
    with pytest.raises(HTTPException) as captured:
        codec.verify("anything")
    assert captured.value.status_code == 503


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        "[]",
        '[{"kid": "a"}]',
        '[{"kid": "a", "private_key_pem": "-----BEGIN PRIVATE KEY-----\\nbad"}]',
    ],
)
def test_signing_key_configuration_is_validated(raw: str) -> None:
    with pytest.raises(ValueError):
        load_signing_keys(raw)


def test_signing_key_ids_must_be_unique() -> None:
    entry = json.loads(generate_signing_keys_json("dup"))[0]
    with pytest.raises(ValueError, match="unique"):
        load_signing_keys(json.dumps([entry, entry]))
