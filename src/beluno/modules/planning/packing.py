"""Packing lists: a shared list for the trip, and each person's private one.

Shared items sync in the plan scope; anyone in the trip marks them packed, and
their creator or an organiser edits them. Private items belong to their owner
alone (RLS hides them from everyone else) and sync in the owner's user scope;
the owner may move one to the shared list, never the other way.

Templates come from the client (already localised): applying one inserts its
items once per plan, owner (or the shared list), and template; applying it again
returns the items already there.

A claiming guest's private items move to their account; a deleted account's are
removed (``transfer_private_items``, ``forget_private_items``).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from beluno.authorization.policy import AccessState, PlanAction
from beluno.contracts.errors import (
    conflict,
    forbidden,
    not_found,
    validation_error,
    version_conflict,
)
from beluno.db.ids import new_id
from beluno.db.models.coordination import PackingItem, TemplateApplication
from beluno.db.models.plans import PlanParticipant
from beluno.modules.context import CommandContext
from beluno.modules.planning.common import planning_access, require_author_or_manager
from beluno.modules.sync_audit.recorder import ChangeScope, record_change, record_mutation

PACKING_ENTITY = "packing_item"
SHARED, PRIVATE = "shared", "private"


@dataclass(frozen=True)
class PackingDraft:
    name: str
    category: str = "other"
    quantity: int = 1
    bringer_participant_id: UUID | None = None


async def list_items(ctx: CommandContext, plan_id: UUID) -> list[PackingItem]:
    """The shared list and the caller's own private items (RLS hides the rest)."""

    await planning_access(ctx, plan_id, PlanAction.VIEW)
    rows = await ctx.session.execute(
        select(PackingItem)
        .where(PackingItem.plan_id == plan_id, PackingItem.deleted_at.is_(None))
        .order_by(PackingItem.visibility, PackingItem.category, PackingItem.id)
    )
    return list(rows.scalars())


async def create_item(
    ctx: CommandContext,
    plan_id: UUID,
    item_id: UUID | None,
    visibility: str,
    draft: PackingDraft,
    *,
    template_id: str | None = None,
) -> PackingItem:
    await _access_for(ctx, plan_id, visibility)
    entry = await _new_item(ctx, plan_id, item_id, visibility, draft, template_id)
    try:
        async with ctx.savepoint():
            ctx.session.add(entry)
            await ctx.session.flush()
    except IntegrityError as error:
        raise conflict("ALREADY_EXISTS", "A resource with this id already exists") from error
    await _record(ctx, entry, "planning.packing_added")
    return entry


async def update_item(
    ctx: CommandContext, plan_id: UUID, item_id: UUID, expected_version: int, draft: PackingDraft
) -> PackingItem:
    entry = await _find(ctx, plan_id, item_id, for_update=True)
    await _require_editor(ctx, entry)
    if entry.version != expected_version:
        raise version_conflict(entry)
    await _apply(ctx, entry, draft)
    await _bump(ctx, entry, "planning.packing_updated")
    return entry


async def set_packed(
    ctx: CommandContext, plan_id: UUID, item_id: UUID, packed: bool
) -> PackingItem:
    """Anyone in the trip ticks a shared item; a private one is its owner's to tick."""

    await planning_access(ctx, plan_id, PlanAction.RESPOND_PLANNING)
    entry = await _find(ctx, plan_id, item_id, for_update=True)
    if entry.packed != packed:
        entry.packed = packed
        await _bump(ctx, entry, "planning.packing_packed")
    return entry


async def share_item(ctx: CommandContext, plan_id: UUID, item_id: UUID) -> PackingItem:
    """Move one of the caller's private items to the shared list."""

    await planning_access(ctx, plan_id, PlanAction.CONTRIBUTE_PLANNING)
    entry = await _find(ctx, plan_id, item_id, for_update=True)
    if entry.visibility == SHARED:
        return entry
    owner = entry.owner_user_id
    assert owner is not None
    entry.visibility, entry.owner_user_id = SHARED, None
    entry.version += 1
    entry.updated_at = ctx.now
    await ctx.session.flush()
    # It leaves the owner's private list (user scope) and joins the trip's.
    await record_mutation(
        ctx,
        action="planning.packing_shared",
        entity_type=PACKING_ENTITY,
        entity_id=entry.id,
        entity_version=entry.version,
        scope=ChangeScope.USER,
        scope_id=owner,
        plan_id=plan_id,
        operation="delete",
    )
    await _record(ctx, entry, "planning.packing_shared")
    return entry


async def delete_item(ctx: CommandContext, plan_id: UUID, item_id: UUID) -> None:
    entry = await _find(ctx, plan_id, item_id, for_update=True)
    await _require_editor(ctx, entry)
    entry.deleted_at = ctx.now
    await _bump(ctx, entry, "planning.packing_deleted", operation="delete")


async def apply_template(
    ctx: CommandContext,
    plan_id: UUID,
    template_id: str,
    visibility: str,
    drafts: list[PackingDraft],
) -> list[PackingItem]:
    """Insert a template's items once for this list; again returns what is there."""

    await _access_for(ctx, plan_id, visibility)
    actor = ctx.require_actor().user_id
    owner = actor if visibility == PRIVATE else None
    try:
        async with ctx.savepoint():
            ctx.session.add(
                TemplateApplication(
                    id=new_id(),
                    plan_id=plan_id,
                    owner_user_id=owner,
                    template_id=template_id,
                    applied_by_user_id=actor,
                    applied_at=ctx.now,
                )
            )
            await ctx.session.flush()
    except IntegrityError:
        rows = await ctx.session.execute(
            select(PackingItem)
            .where(
                PackingItem.plan_id == plan_id,
                PackingItem.template_id == template_id,
                PackingItem.visibility == visibility,
                PackingItem.owner_user_id.is_(None)
                if owner is None
                else PackingItem.owner_user_id == owner,
                PackingItem.deleted_at.is_(None),
            )
            .order_by(PackingItem.id)
        )
        return list(rows.scalars())
    entries = [
        await _new_item(ctx, plan_id, None, visibility, draft, template_id) for draft in drafts
    ]
    ctx.session.add_all(entries)
    await ctx.session.flush()
    for entry in entries:
        await _record(ctx, entry, "planning.packing_added")
    return entries


async def _new_item(
    ctx: CommandContext,
    plan_id: UUID,
    item_id: UUID | None,
    visibility: str,
    draft: PackingDraft,
    template_id: str | None,
) -> PackingItem:
    actor = ctx.require_actor().user_id
    entry = PackingItem(
        id=item_id or new_id(),
        plan_id=plan_id,
        visibility=visibility,
        owner_user_id=actor if visibility == PRIVATE else None,
        packed=False,
        template_id=template_id,
        created_by_user_id=actor,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
        deleted_at=None,
    )
    await _apply(ctx, entry, draft)
    return entry


async def _access_for(ctx: CommandContext, plan_id: UUID, visibility: str) -> None:
    # The shared list takes contributors; a private list is anyone's own.
    action = PlanAction.CONTRIBUTE_PLANNING if visibility == SHARED else PlanAction.RESPOND_PLANNING
    await planning_access(ctx, plan_id, action)


async def _require_editor(ctx: CommandContext, entry: PackingItem) -> None:
    if entry.visibility == PRIVATE:
        if entry.owner_user_id != ctx.require_actor().user_id:
            raise forbidden()
        await planning_access(ctx, entry.plan_id, PlanAction.RESPOND_PLANNING)
        return
    access = await planning_access(ctx, entry.plan_id, PlanAction.VIEW)
    await require_author_or_manager(ctx, access, entry.created_by_user_id)


async def _apply(ctx: CommandContext, entry: PackingItem, draft: PackingDraft) -> None:
    if draft.bringer_participant_id is not None:
        if entry.visibility == PRIVATE:
            raise validation_error("only shared items have someone bringing them")
        if draft.bringer_participant_id != entry.bringer_participant_id:
            found = await ctx.session.scalar(
                select(PlanParticipant.id).where(
                    PlanParticipant.plan_id == entry.plan_id,
                    PlanParticipant.id == draft.bringer_participant_id,
                    PlanParticipant.access_state == AccessState.ACTIVE.value,
                )
            )
            if found is None:
                raise validation_error("bringer_participant_id is not an active participant")
    entry.name = draft.name
    entry.category = draft.category
    entry.quantity = draft.quantity
    entry.bringer_participant_id = draft.bringer_participant_id


async def _find(
    ctx: CommandContext, plan_id: UUID, item_id: UUID, *, for_update: bool = False
) -> PackingItem:
    statement = select(PackingItem).where(PackingItem.plan_id == plan_id, PackingItem.id == item_id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    entry = (await ctx.session.execute(statement)).scalar_one_or_none()
    if entry is None or entry.deleted_at is not None:
        raise not_found()
    return entry


async def _bump(
    ctx: CommandContext, entry: PackingItem, action: str, *, operation: str = "upsert"
) -> None:
    entry.version += 1
    entry.updated_at = ctx.now
    await ctx.session.flush()
    await _record(ctx, entry, action, operation=operation)


async def _record(
    ctx: CommandContext, entry: PackingItem, action: str, *, operation: str = "upsert"
) -> None:
    """Shared items change in the plan scope; private ones in their owner's user scope."""

    private = entry.visibility == PRIVATE
    await record_mutation(
        ctx,
        action=action,
        entity_type=PACKING_ENTITY,
        entity_id=entry.id,
        entity_version=entry.version,
        scope=ChangeScope.USER if private else ChangeScope.PLAN,
        scope_id=entry.owner_user_id if private and entry.owner_user_id else entry.plan_id,
        plan_id=entry.plan_id,
        metadata={"version": entry.version, "visibility": entry.visibility},
        operation=operation,
    )


async def transfer_private_items(ctx: CommandContext, guest_user_id: UUID, target: UUID) -> None:
    """Move the acting guest's private items to ``target``, the account they claimed."""

    moved = await ctx.session.execute(
        text(
            "SELECT item_id, plan_id, item_version FROM coordination.transfer_private_packing(:t)"
        ),
        {"t": target},
    )
    for item_id, plan_id, version in moved.all():
        # Off the guest's devices, onto the account's.
        await record_change(
            ctx,
            entity_type=PACKING_ENTITY,
            entity_id=item_id,
            entity_version=version,
            scope=ChangeScope.USER,
            scope_id=guest_user_id,
            operation="delete",
        )
        await record_mutation(
            ctx,
            action="planning.packing_claimed",
            entity_type=PACKING_ENTITY,
            entity_id=item_id,
            entity_version=version,
            scope=ChangeScope.USER,
            scope_id=target,
            plan_id=plan_id,
            metadata={"version": version},
        )


async def forget_private_items(ctx: CommandContext) -> None:
    """Remove the acting account's private packing lists (account deletion)."""

    await ctx.session.execute(text("SELECT coordination.forget_private_packing()"))
