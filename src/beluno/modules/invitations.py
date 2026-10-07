"""Public entrypoints for plan invite links.

The token is resolved under a transaction-local RLS context that exposes only
the matching invite (and the plan it belongs to). Every unusable token
gets the same ``INVITE_UNAVAILABLE`` response.
"""

from __future__ import annotations

from dataclasses import dataclass

from beluno.contracts.errors import feature_disabled, invite_unavailable
from beluno.db.models.plans import PlanInvite
from beluno.db.roles import set_invite_context
from beluno.modules.context import CommandContext
from beluno.modules.iam.sessions import DeviceInfo
from beluno.modules.invite_links import log_unavailable, token_digest, unavailable_reason
from beluno.modules.plans import invites as plan_invites


@dataclass(frozen=True)
class InvitePreview:
    plan: plan_invites.PlanInvitePreview


@dataclass(frozen=True)
class InviteRedemption:
    plan: plan_invites.PlanRedemption


async def _digest(ctx: CommandContext, raw_token: str) -> bytes:
    if not ctx.settings.invites_enabled:
        raise feature_disabled()
    digest = token_digest(ctx.runtime.require_hasher(), raw_token)
    await set_invite_context(ctx.session, digest)
    return digest


async def preview_invite(ctx: CommandContext, raw_token: str) -> InvitePreview:
    digest = await _digest(ctx, raw_token)
    plan_invite = await plan_invites.find_by_digest(ctx, digest, for_update=False)
    if plan_invite is not None:
        _require_usable(ctx, plan_invite, "preview")
        return InvitePreview(plan=await plan_invites.preview(ctx, plan_invite))
    log_unavailable("unknown", "preview")
    raise invite_unavailable()


async def redeem_invite(
    ctx: CommandContext,
    raw_token: str,
    *,
    display_name: str | None,
    merge_existing: bool,
    device: DeviceInfo,
    avatar_color: str | None = None,
) -> InviteRedemption:
    digest = await _digest(ctx, raw_token)
    plan_invite = await plan_invites.find_by_digest(ctx, digest, for_update=True)
    if plan_invite is not None:
        _require_usable(ctx, plan_invite, "redeem")
        redemption = await plan_invites.redeem(
            ctx,
            plan_invite,
            display_name=display_name,
            merge_existing=merge_existing,
            device=device,
            avatar_color=avatar_color,
        )
        return InviteRedemption(plan=redemption)
    log_unavailable("unknown", "redeem")
    raise invite_unavailable()


def _require_usable(
    ctx: CommandContext,
    invite: PlanInvite,
    operation: str,
) -> None:
    reason = unavailable_reason(invite, ctx.now)
    if reason is not None:
        log_unavailable(reason, operation)
        raise invite_unavailable()
