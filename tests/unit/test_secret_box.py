from __future__ import annotations

import base64
import json
import os
from uuid import uuid4

import pytest

from beluno.secret_box import SecretBox, SecretBoxError


def keyring(*names: str) -> dict[str, bytes]:
    return {name: os.urandom(32) for name in names}


def test_secrets_round_trip_and_stay_bound_to_their_row_and_field() -> None:
    keys = keyring("k1")
    box = SecretBox("k1", keys)
    owner, other = uuid4(), uuid4()
    sealed = box.seal("ABC-123", owner=owner, field="confirmation_code")
    assert b"ABC-123" not in sealed.blob
    assert box.open(sealed, owner=owner, field="confirmation_code") == "ABC-123"
    # Same text twice: different ciphertexts (fresh nonce).
    assert box.seal("ABC-123", owner=owner, field="confirmation_code").blob != sealed.blob
    for wrong_owner, wrong_field in ((other, "confirmation_code"), (owner, "private_notes")):
        with pytest.raises(SecretBoxError):
            box.open(sealed, owner=wrong_owner, field=wrong_field)
    tampered = type(sealed)(sealed.key_id, sealed.blob[:-1] + bytes([sealed.blob[-1] ^ 1]))
    with pytest.raises(SecretBoxError):
        box.open(tampered, owner=owner, field="confirmation_code")


def test_rotation_keeps_old_secrets_readable_and_writes_with_the_new_key() -> None:
    keys = keyring("k1", "k2")
    old_box, new_box = SecretBox("k1", keys), SecretBox("k2", keys)
    owner = uuid4()
    old = old_box.seal("old", owner=owner, field="private_notes")
    assert new_box.open(old, owner=owner, field="private_notes") == "old"
    assert new_box.seal("new", owner=owner, field="private_notes").key_id == "k2"
    with pytest.raises(SecretBoxError):
        SecretBox("k2", {"k2": keys["k2"]}).open(old, owner=owner, field="private_notes")


def test_a_malformed_keyring_is_refused() -> None:
    good = base64.b64encode(os.urandom(32)).decode()
    for document in (
        "not json",
        json.dumps({"active": "k9", "keys": {"k1": good}}),
        json.dumps({"active": "k1", "keys": {"k1": base64.b64encode(b"short").decode()}}),
        json.dumps({"active": "k1", "keys": {"k1": "%%%"}}),
        json.dumps({"active": "k" * 65, "keys": {"k" * 65: good}}),
    ):
        with pytest.raises(SecretBoxError):
            SecretBox.from_json(document)
    assert (
        SecretBox.from_json(json.dumps({"active": "k1", "keys": {"k1": good}})).active_key_id
        == "k1"
    )
