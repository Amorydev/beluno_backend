"""Passwordless email sign-in: one-time code plus magic link.

The API only records a pending challenge and enqueues delivery in the same
transaction. The worker generates the code and link token, stores their HMAC
digests, then sends the email, so neither secret is ever persisted or queued in
plaintext. Responses never reveal whether an email already has an account.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import select

from beluno.db.ids import new_id
from beluno.db.models.iam import EmailChallenge
from beluno.modules.context import CommandContext, Runtime
from beluno.modules.iam.email_delivery import EmailSender, OutboundEmail
from beluno.modules.iam.external_identity import IdentityProvider, VerifiedIdentity
from beluno.token_hashing import new_numeric_code, new_secret_token
from beluno.worker.enqueue import defer_in_transaction

CODE_PURPOSE = "email_code"
LINK_PURPOSE = "email_link"
CHALLENGE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
DELIVER_TASK_NAME = "iam.deliver_email_challenge"
EMAIL_QUEUE = "email"


async def start_challenge(ctx: CommandContext, email: str) -> EmailChallenge:
    challenge = EmailChallenge(
        id=new_id(),
        email=email,
        code_hash=None,
        link_token_hash=None,
        delivery_state="pending",
        delivered_at=None,
        failed_attempts=0,
        max_attempts=MAX_ATTEMPTS,
        expires_at=ctx.now + CHALLENGE_TTL,
        consumed_at=None,
        created_at=ctx.now,
    )
    ctx.session.add(challenge)
    await ctx.session.flush()
    await defer_in_transaction(
        ctx.session,
        task_name=DELIVER_TASK_NAME,
        queue=EMAIL_QUEUE,
        args={"challenge_id": str(challenge.id), "payload_version": 1},
        queueing_lock=f"email_challenge:{challenge.id}",
    )
    return challenge


async def deliver_challenge(runtime: Runtime, challenge_id: UUID, sender: EmailSender) -> bool:
    """Generate secrets, persist digests, then send. Returns False when nothing to send.

    Retries regenerate the secrets, so a code from a failed attempt stops working.
    """

    hasher = runtime.require_hasher()
    now = runtime.clock()
    code = new_numeric_code()
    link_token = new_secret_token()
    async with runtime.database.transaction() as session:
        challenge = (
            await session.execute(
                select(EmailChallenge).where(EmailChallenge.id == challenge_id).with_for_update()
            )
        ).scalar_one_or_none()
        if (
            challenge is None
            or challenge.delivery_state == "sent"
            or challenge.consumed_at is not None
            or challenge.expires_at <= now
        ):
            return False
        challenge.code_hash = hasher.digest(CODE_PURPOSE, f"{challenge.id}:{code}")
        challenge.link_token_hash = hasher.digest(LINK_PURPOSE, link_token)
        challenge.delivery_state = "sending"
        challenge.failed_attempts = 0
        recipient = challenge.email
    await sender.send(_compose_message(runtime, recipient, code, link_token))
    async with runtime.database.transaction() as session:
        delivered = await session.get(EmailChallenge, challenge_id, with_for_update=True)
        if delivered is not None and delivered.delivery_state == "sending":
            delivered.delivery_state = "sent"
            delivered.delivered_at = runtime.clock()
    return True


async def verify_code(
    ctx: CommandContext,
    challenge_id: UUID,
    code: str,
) -> VerifiedIdentity | None:
    """Return the proven email, or ``None``; wrong codes are counted and committed."""

    hasher = ctx.runtime.require_hasher()
    challenge = (
        await ctx.session.execute(
            select(EmailChallenge).where(EmailChallenge.id == challenge_id).with_for_update()
        )
    ).scalar_one_or_none()
    if challenge is None or not _is_redeemable(ctx, challenge) or challenge.code_hash is None:
        return None
    if not hasher.matches(CODE_PURPOSE, f"{challenge_id}:{code}", challenge.code_hash):
        challenge.failed_attempts += 1
        return None
    return _consume(ctx, challenge)


async def verify_link(ctx: CommandContext, link_token: str) -> VerifiedIdentity | None:
    hasher = ctx.runtime.require_hasher()
    challenge = (
        await ctx.session.execute(
            select(EmailChallenge)
            .where(EmailChallenge.link_token_hash == hasher.digest(LINK_PURPOSE, link_token))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if challenge is None or not _is_redeemable(ctx, challenge):
        return None
    return _consume(ctx, challenge)


def _is_redeemable(ctx: CommandContext, challenge: EmailChallenge) -> bool:
    return (
        challenge.consumed_at is None
        and challenge.expires_at > ctx.now
        and challenge.failed_attempts < challenge.max_attempts
    )


def _consume(ctx: CommandContext, challenge: EmailChallenge) -> VerifiedIdentity:
    challenge.consumed_at = ctx.now
    return VerifiedIdentity(
        provider=IdentityProvider.EMAIL,
        subject=challenge.email,
        email=challenge.email,
        email_verified=True,
    )


def _compose_message(runtime: Runtime, recipient: str, code: str, link_token: str) -> OutboundEmail:
    lines = [
        f"Your Beluno sign-in code is {code}.",
        "",
        "It expires in 10 minutes. If you did not ask to sign in, ignore this email.",
    ]
    magic_link_url = runtime.settings.auth_magic_link_url
    if magic_link_url:
        lines[1:1] = ["", f"Or open this link on your device: {magic_link_url}#token={link_token}"]
    return OutboundEmail(
        to=recipient, subject="Your Beluno sign-in code", text_body="\n".join(lines)
    )
