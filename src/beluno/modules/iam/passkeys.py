"""Passkeys (WebAuthn): registered accounts add them, then sign in or step up with them.

* **Register** (signed in, recent sign-in, not a guest): the server issues a
  single-use challenge, the device creates a discoverable credential with user
  verification, and the server verifies it (no attestation is requested or kept).
* **Authenticate**: a challenge (bound to the caller when signed in), the device
  signs it, and the server checks the signature, the counter, and user
  verification. The result is a ``VerifiedIdentity`` of provider ``passkey`` whose
  subject is the account, so signing in, stepping up, and a guest claiming the
  account all go through ``sign_in.authenticate`` like any other method.

Challenges are stored only as HMAC digests and expire after five minutes. A
passkey never creates an account: every account keeps another way to sign in.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from psycopg.errors import UniqueViolation
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import (
    base64url_to_bytes,
    bytes_to_base64url,
    decode_credential_public_key,
    decoded_public_key_to_cryptography,
    parse_client_data_json,
)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from beluno.contracts.errors import (
    conflict,
    forbidden,
    not_found,
    step_up_required,
)
from beluno.db.ids import new_id
from beluno.db.models.iam import Passkey, WebAuthnChallenge
from beluno.modules.context import CommandContext
from beluno.modules.iam import users
from beluno.modules.iam.external_identity import IdentityProvider, VerifiedIdentity
from beluno.modules.sync_audit.recorder import record_audit

CHALLENGE_TTL = timedelta(minutes=5)
CHALLENGE_PURPOSE = "webauthn_challenge"
REGISTER, AUTHENTICATE = "register", "authenticate"
TIMEOUT_MS = int(CHALLENGE_TTL.total_seconds() * 1000)
KNOWN_TRANSPORTS = frozenset(transport.value for transport in AuthenticatorTransport)
# Anything the library or a malformed body can throw means the response does not verify.
MALFORMED = (WebAuthnException, KeyError, TypeError, ValueError, AttributeError)


@dataclass(frozen=True)
class Options:
    challenge_id: UUID
    public_key: dict[str, Any]  # PublicKeyCredential{Creation,Request}Options as JSON


async def registration_options(ctx: CommandContext) -> Options:
    user_id = _require_registered(ctx)
    if not ctx.step_up_is_fresh:
        raise step_up_required()
    user = await users.load_user(ctx, user_id)
    challenge = secrets.token_bytes(32)
    options = generate_registration_options(
        rp_id=ctx.settings.webauthn_rp_id,
        rp_name=ctx.settings.webauthn_rp_name,
        # The account ID is opaque; the name helps the person pick it in their manager.
        user_id=user.id.bytes,
        user_name=user.email or user.display_name,
        user_display_name=user.display_name,
        challenge=challenge,
        timeout=TIMEOUT_MS,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=key.credential_id)
            for key in await _own_passkeys(ctx, user_id)
        ],
    )
    challenge_id = await _store_challenge(ctx, REGISTER, user_id, challenge)
    return Options(challenge_id, json.loads(options_to_json(options)))


async def register(
    ctx: CommandContext, challenge_id: UUID, credential: dict[str, Any], label: str
) -> Passkey | None:
    """The new passkey, or ``None`` when the response does not verify (the challenge is
    still used up: the caller commits and then refuses)."""

    user_id = _require_registered(ctx)
    if not ctx.step_up_is_fresh:
        raise step_up_required()
    challenge = await _consume_challenge(ctx, challenge_id, REGISTER, credential, user_id)
    if challenge is None:
        return None
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=ctx.settings.webauthn_rp_id,
            expected_origin=ctx.settings.webauthn_origins,
            require_user_verification=True,
        )
    except MALFORMED:
        return None
    if not _usable_public_key(verified.credential_public_key):
        return None
    transports = _transports(credential)
    passkey = Passkey(
        id=new_id(),
        user_id=user_id,
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=transports,
        label=label,
        backed_up=verified.credential_backed_up,
        created_at=ctx.now,
        last_used_at=None,
    )
    try:
        async with ctx.savepoint():
            ctx.session.add(passkey)
            await ctx.session.flush()
    except IntegrityError as error:
        if isinstance(error.orig, UniqueViolation):
            raise conflict("PASSKEY_ALREADY_REGISTERED", "This passkey is already added") from error
        return None
    await record_audit(ctx, action="iam.passkey_added", entity_type="passkey", entity_id=passkey.id)
    return passkey


async def authentication_options(ctx: CommandContext) -> Options:
    """Signed out or a guest: any passkey of the site may answer. Registered: one of
    the caller's (step-up). The challenge is bound to the caller either way."""

    actor = ctx.actor
    # Bound to whoever asked (guests too): only they may answer it.
    user_id = actor.user_id if actor is not None else None
    challenge = secrets.token_bytes(32)
    # A registered caller steps up with one of theirs; anyone else picks a passkey.
    allowed = (
        [
            PublicKeyCredentialDescriptor(id=key.credential_id)
            for key in await _own_passkeys(ctx, actor.user_id)
        ]
        if actor is not None and not actor.is_guest
        else None
    )
    options = generate_authentication_options(
        rp_id=ctx.settings.webauthn_rp_id,
        challenge=challenge,
        timeout=TIMEOUT_MS,
        allow_credentials=allowed,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    challenge_id = await _store_challenge(ctx, AUTHENTICATE, user_id, challenge)
    return Options(challenge_id, json.loads(options_to_json(options)))


async def verify(
    ctx: CommandContext, challenge_id: UUID, credential: dict[str, Any]
) -> VerifiedIdentity | None:
    """The identity a passkey assertion proves (its account), or ``None`` when it does
    not verify; the challenge is used up either way once the caller commits."""

    actor = ctx.actor
    bound = actor.user_id if actor is not None else None
    challenge = await _consume_challenge(ctx, challenge_id, AUTHENTICATE, credential, bound)
    if challenge is None:
        return None
    try:
        raw_id = base64url_to_bytes(str(credential["rawId"]))
    except MALFORMED:
        return None
    passkey = (
        await ctx.session.execute(
            select(Passkey).where(Passkey.credential_id == raw_id).with_for_update()
        )
    ).scalar_one_or_none()
    if passkey is None:
        return None
    handle = credential.get("response", {}).get("userHandle")
    try:
        if handle and base64url_to_bytes(str(handle)) != passkey.user_id.bytes:
            return None
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=ctx.settings.webauthn_rp_id,
            expected_origin=ctx.settings.webauthn_origins,
            credential_public_key=passkey.public_key,
            credential_current_sign_count=passkey.sign_count,
            require_user_verification=True,
        )
    except MALFORMED:
        return None
    passkey.sign_count = verified.new_sign_count
    passkey.backed_up = verified.credential_backed_up
    passkey.last_used_at = ctx.now
    await ctx.session.flush()
    return VerifiedIdentity(
        provider=IdentityProvider.PASSKEY,
        subject=str(passkey.user_id),
        email=None,
        email_verified=False,
    )


async def list_passkeys(ctx: CommandContext) -> list[Passkey]:
    return await _own_passkeys(ctx, ctx.require_actor().user_id)


async def rename(ctx: CommandContext, passkey_id: UUID, label: str) -> Passkey:
    passkey = await _own_passkey(ctx, passkey_id)
    passkey.label = label
    await ctx.session.flush()
    return passkey


async def remove(ctx: CommandContext, passkey_id: UUID) -> None:
    """Removing a sign-in method needs a recent sign-in, as adding one does."""

    passkey = await _own_passkey(ctx, passkey_id)
    if not ctx.step_up_is_fresh:
        raise step_up_required()
    await ctx.session.delete(passkey)
    await ctx.session.flush()
    await record_audit(
        ctx, action="iam.passkey_removed", entity_type="passkey", entity_id=passkey_id
    )


def _require_registered(ctx: CommandContext) -> UUID:
    actor = ctx.require_actor()
    if actor.is_guest:
        raise forbidden("Create an account before adding a passkey")
    return actor.user_id


async def _own_passkeys(ctx: CommandContext, user_id: UUID) -> list[Passkey]:
    rows = await ctx.session.execute(
        select(Passkey).where(Passkey.user_id == user_id).order_by(Passkey.created_at, Passkey.id)
    )
    return list(rows.scalars())


async def _own_passkey(ctx: CommandContext, passkey_id: UUID) -> Passkey:
    passkey = await ctx.session.get(Passkey, passkey_id)
    if passkey is None or passkey.user_id != ctx.require_actor().user_id:
        raise not_found()
    return passkey


async def _store_challenge(
    ctx: CommandContext, purpose: str, user_id: UUID | None, challenge: bytes
) -> UUID:
    row = WebAuthnChallenge(
        id=new_id(),
        purpose=purpose,
        user_id=user_id,
        challenge_hash=_digest(ctx, challenge),
        expires_at=ctx.now + CHALLENGE_TTL,
        consumed_at=None,
        created_at=ctx.now,
    )
    ctx.session.add(row)
    await ctx.session.flush()
    return row.id


async def _consume_challenge(
    ctx: CommandContext,
    challenge_id: UUID,
    purpose: str,
    credential: dict[str, Any],
    user_id: UUID | None,
) -> bytes | None:
    """The challenge the response signed, if it is the one issued here, unused, and live.

    Marked used before the response is verified; callers report a failed verification
    only after committing, so a challenge never serves two attempts.
    """

    try:
        client_data = parse_client_data_json(
            base64url_to_bytes(str(credential["response"]["clientDataJSON"]))
        )
    except MALFORMED:
        return None
    if client_data.cross_origin:
        return None
    row = (
        await ctx.session.execute(
            select(WebAuthnChallenge)
            .where(WebAuthnChallenge.id == challenge_id, WebAuthnChallenge.purpose == purpose)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        row is None
        or row.consumed_at is not None
        or row.expires_at <= ctx.now
        or row.user_id != user_id
        or not ctx.runtime.require_hasher().matches(
            CHALLENGE_PURPOSE, bytes_to_base64url(client_data.challenge), row.challenge_hash
        )
    ):
        return None
    row.consumed_at = ctx.now
    await ctx.session.flush()
    return client_data.challenge


def _digest(ctx: CommandContext, challenge: bytes) -> bytes:
    return ctx.runtime.require_hasher().digest(CHALLENGE_PURPOSE, bytes_to_base64url(challenge))


def _usable_public_key(public_key: bytes) -> bool:
    """A key the server can verify with later (supported type, curve, and algorithm)."""

    try:
        decoded_public_key_to_cryptography(decode_credential_public_key(public_key))
    except MALFORMED:
        return False
    return True


def _transports(credential: dict[str, Any]) -> list[str]:
    response = credential.get("response")
    found = response.get("transports") if isinstance(response, dict) else None
    if not isinstance(found, list):
        return []
    return sorted({t for t in found if isinstance(t, str) and t in KNOWN_TRANSPORTS})
