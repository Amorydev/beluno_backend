"""Encrypt small secrets at rest (booking confirmation codes and private notes).

AES-256-GCM with a fresh 96-bit nonce per encryption. The associated data binds a
ciphertext to its row and field, so a value copied into another booking or field
fails to decrypt. Keys come from ``BELUNO_BOOKING_KEYS``::

    {"active": "k2", "keys": {"k1": "<base64 32 bytes>", "k2": "<base64 32 bytes>"}}

A ciphertext remembers its key ID: old keys keep decrypting, and every write
encrypts with the active key, so rotation is "add a key, make it active".
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from dataclasses import dataclass
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES = 12
KEY_BYTES = 32


class SecretBoxError(ValueError):
    """The keyring is malformed, a key is unknown, or a ciphertext was tampered with."""


@dataclass(frozen=True)
class Sealed:
    key_id: str
    blob: bytes  # nonce || ciphertext || tag


class SecretBox:
    def __init__(self, active: str, keys: dict[str, bytes]) -> None:
        if active not in keys:
            raise SecretBoxError("the active key is not in the keyring")
        for key_id, key in keys.items():
            if not 1 <= len(key_id) <= 64 or len(key) != KEY_BYTES:
                raise SecretBoxError(f"key ids are 1 to 64 characters, keys {KEY_BYTES} bytes")
        self._active = active
        self._keys = {key_id: AESGCM(key) for key_id, key in keys.items()}

    @classmethod
    def from_json(cls, document: str) -> SecretBox:
        try:
            parsed = json.loads(document)
            keys = {
                str(key_id): base64.b64decode(value, validate=True)
                for key_id, value in parsed["keys"].items()
            }
            return cls(str(parsed["active"]), keys)
        except (ValueError, KeyError, TypeError, AttributeError, binascii.Error) as error:
            raise SecretBoxError("BELUNO_BOOKING_KEYS is not a valid keyring") from error

    @property
    def active_key_id(self) -> str:
        return self._active

    def seal(self, plaintext: str, *, owner: UUID, field: str) -> Sealed:
        nonce = os.urandom(NONCE_BYTES)
        sealed = self._keys[self._active].encrypt(
            nonce, plaintext.encode("utf-8"), _context(owner, field)
        )
        return Sealed(self._active, nonce + sealed)

    def open(self, sealed: Sealed, *, owner: UUID, field: str) -> str:
        cipher = self._keys.get(sealed.key_id)
        if cipher is None:
            raise SecretBoxError(f"unknown key {sealed.key_id!r}")
        nonce, body = sealed.blob[:NONCE_BYTES], sealed.blob[NONCE_BYTES:]
        try:
            return cipher.decrypt(nonce, body, _context(owner, field)).decode("utf-8")
        except InvalidTag as error:
            raise SecretBoxError("the secret does not belong here or was altered") from error


def _context(owner: UUID, field: str) -> bytes:
    return f"beluno:{field}:{owner}".encode()


def new_keyring_json(key_id: str) -> str:
    """A one-key keyring for ``BELUNO_BOOKING_KEYS`` (local setup and tests)."""

    key = base64.b64encode(os.urandom(KEY_BYTES)).decode("ascii")
    return json.dumps({"active": key_id, "keys": {key_id: key}})
