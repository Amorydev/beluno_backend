"""Application users and their links to verified identities."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from beluno.contracts.errors import conflict, version_conflict
from beluno.db.ids import new_id
from beluno.db.models.iam import User, UserIdentity
from beluno.modules.context import CommandContext
from beluno.modules.iam.external_identity import IdentityProvider, VerifiedIdentity
from beluno.modules.sync_audit.recorder import ChangeScope, record_mutation

REGISTERED = "registered"
GUEST = "guest"
DEFAULT_DISPLAY_NAME = "Beluno member"


async def find_user_id(ctx: CommandContext, identity: VerifiedIdentity) -> UUID | None:
    """Resolve an identity to its account without needing RLS visibility of the user."""

    if identity.provider is IdentityProvider.EMAIL:
        return await find_user_id_by_email(ctx, identity.subject)
    return (
        await ctx.session.execute(
            select(UserIdentity.user_id).where(
                UserIdentity.provider == identity.provider.value,
                UserIdentity.subject == identity.subject,
            )
        )
    ).scalar_one_or_none()


async def find_user_id_by_email(ctx: CommandContext, email: str) -> UUID | None:
    result = await ctx.session.execute(
        text("SELECT iam.resolve_user_by_email(:email)"), {"email": email}
    )
    value = result.scalar_one_or_none()
    return value if isinstance(value, UUID) else None


async def load_user(ctx: CommandContext, user_id: UUID, *, for_update: bool = False) -> User:
    statement = select(User).where(User.id == user_id)
    if for_update:
        statement = statement.with_for_update()
    user = (await ctx.session.execute(statement)).scalar_one_or_none()
    if user is None:
        raise LookupError("user is not visible in this transaction")
    return user


def initial_display_name(identity: VerifiedIdentity) -> str:
    return identity.display_name or DEFAULT_DISPLAY_NAME


async def _unclaimed_verified_email(ctx: CommandContext, identity: VerifiedIdentity) -> str | None:
    """The identity's verified email, unless another account already uses it."""

    email = identity.email if identity.email_verified else None
    if email is not None and await find_user_id_by_email(ctx, email) is not None:
        return None
    return email


async def create_registered_user(ctx: CommandContext, identity: VerifiedIdentity) -> User:
    """Create an account for a first-time identity and switch the RLS actor to it."""

    email = await _unclaimed_verified_email(ctx, identity)
    user = User(
        id=new_id(),
        kind=REGISTERED,
        status="active",
        display_name=initial_display_name(identity),
        email=email,
        email_verified_at=ctx.now if email else None,
        locale=None,
        timezone=None,
        merged_into_user_id=None,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
    )
    await ctx.act_as(user.id)
    ctx.session.add(user)
    await ctx.session.flush()
    if identity.provider is not IdentityProvider.EMAIL:
        await link_identity(ctx, user.id, identity)
    await _record_user_change(ctx, user, "user.registered", actor_user_id=user.id)
    return user


async def create_guest_user(ctx: CommandContext, display_name: str) -> User:
    user = User(
        id=new_id(),
        kind=GUEST,
        status="active",
        display_name=display_name,
        email=None,
        email_verified_at=None,
        locale=None,
        timezone=None,
        merged_into_user_id=None,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
    )
    await ctx.act_as(user.id)
    ctx.session.add(user)
    await ctx.session.flush()
    await _record_user_change(ctx, user, "user.guest_created", actor_user_id=user.id)
    return user


async def link_identity(ctx: CommandContext, user_id: UUID, identity: VerifiedIdentity) -> None:
    """Attach an external identity; an identity already owned by someone else is a conflict."""

    if identity.provider is IdentityProvider.EMAIL:
        return
    inserted = await ctx.session.execute(
        insert(UserIdentity)
        .values(
            id=new_id(),
            user_id=user_id,
            provider=identity.provider.value,
            subject=identity.subject,
            created_at=ctx.now,
            last_used_at=ctx.now,
        )
        .on_conflict_do_nothing(index_elements=["provider", "subject"])
    )
    if inserted.rowcount == 0:  # type: ignore[attr-defined]
        owner = await find_user_id(ctx, identity)
        if owner != user_id:
            raise conflict("IDENTITY_ALREADY_LINKED", "This sign-in is linked to another account")


async def record_identity_linked(ctx: CommandContext, user: User, provider: str) -> None:
    await _record_user_change(ctx, user, "user.identity_linked", metadata={"provider": provider})


async def touch_identity(ctx: CommandContext, identity: VerifiedIdentity) -> None:
    if identity.provider is IdentityProvider.EMAIL:
        return
    await ctx.session.execute(
        update(UserIdentity)
        .where(
            UserIdentity.provider == identity.provider.value,
            UserIdentity.subject == identity.subject,
        )
        .values(last_used_at=ctx.now)
    )


async def upgrade_guest(ctx: CommandContext, guest: User, identity: VerifiedIdentity) -> User:
    """Turn a guest into a registered account in place; the user ID never changes."""

    email = await _unclaimed_verified_email(ctx, identity)
    await link_identity(ctx, guest.id, identity)
    guest.kind = REGISTERED
    guest.email = email
    guest.email_verified_at = ctx.now if email else None
    if guest.display_name == DEFAULT_DISPLAY_NAME and identity.display_name:
        guest.display_name = identity.display_name
    guest.version += 1
    guest.updated_at = ctx.now
    await ctx.session.flush()
    await _record_user_change(ctx, guest, "user.guest_upgraded", actor_user_id=guest.id)
    return guest


async def retire_merged_guest(ctx: CommandContext, guest: User, target_user_id: UUID) -> None:
    guest.status = "disabled"
    guest.merged_into_user_id = target_user_id
    guest.version += 1
    guest.updated_at = ctx.now
    await ctx.session.flush()
    await _record_user_change(
        ctx,
        guest,
        "user.guest_merged",
        actor_user_id=guest.id,
        metadata={"target_user_id": str(target_user_id)},
    )


@dataclass(frozen=True)
class ProfileChanges:
    display_name: str | None = None
    locale: str | None = None
    timezone: str | None = None
    clear_locale: bool = False
    clear_timezone: bool = False


async def update_profile(
    ctx: CommandContext,
    changes: ProfileChanges,
    expected_version: int,
) -> User:
    actor = ctx.require_actor()
    user = await load_user(ctx, actor.user_id, for_update=True)
    if user.version != expected_version:
        raise version_conflict()
    if changes.display_name is not None:
        user.display_name = changes.display_name
    if changes.locale is not None or changes.clear_locale:
        user.locale = changes.locale
    if changes.timezone is not None or changes.clear_timezone:
        user.timezone = changes.timezone
    user.version += 1
    user.updated_at = ctx.now
    await ctx.session.flush()
    await _record_user_change(ctx, user, "user.profile_updated")
    return user


async def _record_user_change(
    ctx: CommandContext,
    user: User,
    action: str,
    *,
    actor_user_id: UUID | None = None,
    metadata: dict[str, str] | None = None,
) -> None:
    await record_mutation(
        ctx,
        action=action,
        entity_type="user",
        entity_id=user.id,
        entity_version=user.version,
        scope=ChangeScope.USER,
        scope_id=user.id,
        metadata={"kind": user.kind, **(metadata or {})},
        actor_user_id=actor_user_id,
    )
