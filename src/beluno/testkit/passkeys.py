"""A software passkey authenticator for tests: real ES256 keys, real WebAuthn bytes.

It answers the options the API returns exactly as a platform authenticator would
(``none`` attestation, user presence and verification flags, a signature counter),
so tests exercise the server's real verification, not a stand-in.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, encode_cbor

USER_PRESENT, USER_VERIFIED, BACKUP_ELIGIBLE, BACKED_UP, ATTESTED = 0x01, 0x04, 0x08, 0x10, 0x40


class SoftAuthenticator:
    def __init__(
        self,
        *,
        rp_id: str = "localhost",
        origin: str = "http://localhost",
        synced: bool = False,
        counts: bool = True,
        credential_id_length: int = 32,
    ) -> None:
        self.rp_id = rp_id
        self.origin = origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(credential_id_length)
        self.user_handle: bytes | None = None
        self.synced = synced  # backed up, as iCloud Keychain or Google Password Manager
        self.counts = counts  # synced passkeys report a zero counter
        self.sign_count = 0

    def create(self, public_key: dict[str, Any], *, verified: bool = True) -> dict[str, Any]:
        """A RegistrationResponseJSON for ``navigator.credentials.create``."""

        self.user_handle = base64url_to_bytes(public_key["user"]["id"])
        client_data = self._client_data("webauthn.create", public_key["challenge"])
        numbers = self.key.public_key().public_numbers()
        cose_key = encode_cbor(
            {
                1: 2,  # kty: EC2
                3: -7,  # alg: ES256
                -1: 1,  # crv: P-256
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )
        attested = (
            bytes(16)  # AAGUID: none
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + cose_key
        )
        auth_data = self._auth_data(verified, ATTESTED) + attested
        attestation = encode_cbor({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "attestationObject": bytes_to_base64url(attestation),
                "transports": ["internal", "hybrid"],
            },
            "clientExtensionResults": {},
        }

    def get(
        self,
        public_key: dict[str, Any],
        *,
        verified: bool = True,
        origin: str | None = None,
        sign_count: int | None = None,
    ) -> dict[str, Any]:
        """An AuthenticationResponseJSON for ``navigator.credentials.get``."""

        if sign_count is not None:
            self.sign_count = sign_count
        elif self.counts:
            self.sign_count += 1
        client_data = self._client_data("webauthn.get", public_key["challenge"], origin)
        auth_data = self._auth_data(verified)
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": bytes_to_base64url(self.user_handle) if self.user_handle else None,
            },
            "clientExtensionResults": {},
        }

    def _client_data(self, kind: str, challenge: str, origin: str | None = None) -> bytes:
        document = {"type": kind, "challenge": challenge, "origin": origin or self.origin}
        return json.dumps({**document, "crossOrigin": False}).encode()

    def _auth_data(self, verified: bool, extra: int = 0) -> bytes:
        flags = USER_PRESENT | extra | (USER_VERIFIED if verified else 0)
        if self.synced:
            flags |= BACKUP_ELIGIBLE | BACKED_UP
        return (
            hashlib.sha256(self.rp_id.encode()).digest()
            + bytes([flags])
            + self.sign_count.to_bytes(4, "big")
        )
