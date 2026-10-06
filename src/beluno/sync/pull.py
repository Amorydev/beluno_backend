"""Pull one scope: snapshot bootstrap, then high-watermark change pages.

Bootstrap (``cursor = null``) captures the scope head first, streams the
current rows entity type by entity type, and then continues as a change feed
from that head, so nothing that happened during the snapshot is lost. A change
page reads ``(cursor, watermark]``, collapses repeated entities, loads each
entity's current state through the projector, and returns ``delete`` for rows
that no longer exist or are no longer visible. Replaying a page is safe: the
same sequences come back, with data at least as new.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import text

from beluno.contracts.errors import validation_error
from beluno.db.models.sync_audit import ScopeHead
from beluno.modules.context import CommandContext
from beluno.sync.cursor import Cursor, CursorCodec, CursorError
from beluno.sync.scopes import AccessLevel, ScopeKey, scope_access

READ_CHANGES = text(
    "SELECT scope_seq, entity_type, entity_id, entity_version, operation, changed_at "
    "FROM sync_audit.read_changes(:scope_type, :scope_id, :after, :upto, :limit)"
)
READ_FLOOR = text(
    "SELECT floor_seq FROM sync_audit.scope_heads "
    "WHERE scope_type = :scope_type AND scope_id = :scope_id"
)
# The SQL gate never returns more rows than this; a page plus its lookahead row must fit.
READ_CHANGES_CAP = 1001


class Hidden:
    """The row exists but this access level may not see it; emit nothing."""


HIDDEN = Hidden()


@dataclass(frozen=True)
class SnapshotRow:
    entity_id: UUID
    version: int
    data: BaseModel


class Projector(Protocol):
    """Renders entity state for the feed; implemented over the public presenters."""

    def snapshot_order(self, scope_type: str) -> tuple[str, ...]: ...

    def visible_types(self, scope_type: str, level: AccessLevel) -> frozenset[str]: ...

    async def load(
        self,
        ctx: CommandContext,
        scope: ScopeKey,
        level: AccessLevel,
        entity_type: str,
        entity_id: UUID,
    ) -> BaseModel | Hidden | None: ...

    async def snapshot_page(
        self,
        ctx: CommandContext,
        scope: ScopeKey,
        level: AccessLevel,
        entity_type: str,
        after: UUID | None,
        limit: int,
    ) -> list[SnapshotRow]: ...


@dataclass(frozen=True)
class FeedItem:
    seq: int | None
    entity_type: str
    entity_id: UUID
    operation: Literal["upsert", "delete"]
    version: int | None
    changed_at: datetime | None
    data: dict[str, Any] | None


@dataclass(frozen=True)
class ScopePage:
    scope: ScopeKey
    status: Literal["ok", "unavailable", "resync_required"]
    cursor: str | None = None
    has_more: bool = False
    head: int | None = None
    items: tuple[FeedItem, ...] = ()


async def pull_scope(
    ctx: CommandContext,
    *,
    codec: CursorCodec,
    projector: Projector,
    scope: ScopeKey,
    raw_cursor: str | None,
    page_size: int,
) -> ScopePage:
    actor = ctx.require_actor()
    level = await scope_access(ctx, scope)
    if level is None:
        return ScopePage(scope, "unavailable")
    head = await ctx.session.get(ScopeHead, (scope.scope_type.value, scope.scope_id))
    head_seq, floor, generation = (
        (head.last_seq, head.floor_seq, head.generation) if head else (0, 0, 1)
    )
    if raw_cursor is None:
        cursor = Cursor.snapshot_start(actor.user_id, scope, generation, level, head_seq)
    else:
        try:
            cursor = codec.decode(raw_cursor, user_id=actor.user_id)
        except CursorError:
            return ScopePage(scope, "resync_required", head=head_seq)
        if cursor.scope != scope:
            raise validation_error(f"cursor does not belong to scope {scope}")
        if not _cursor_is_current(cursor, level, generation, floor, head_seq):
            return ScopePage(scope, "resync_required", head=head_seq)
    if cursor.mode == "snapshot":
        return await _snapshot_page(ctx, codec, projector, cursor, head_seq, page_size)
    return await _changes_page(ctx, codec, projector, cursor, head_seq, page_size)


def _cursor_is_current(
    cursor: Cursor, level: AccessLevel, generation: int, floor: int, head_seq: int
) -> bool:
    if cursor.level is not level or cursor.generation != generation:
        return False
    if cursor.mode == "snapshot":
        # ``seq`` is the head captured when the snapshot started.
        return floor <= cursor.seq <= head_seq
    if cursor.seq < floor or cursor.seq > head_seq:
        return False
    return cursor.watermark is None or cursor.seq <= cursor.watermark <= head_seq


async def _snapshot_page(
    ctx: CommandContext,
    codec: CursorCodec,
    projector: Projector,
    cursor: Cursor,
    head_seq: int,
    page_size: int,
) -> ScopePage:
    scope = cursor.scope
    order = projector.snapshot_order(scope.scope_type.value)
    visible = projector.visible_types(scope.scope_type.value, cursor.level)
    phase, after = cursor.phase, cursor.after
    items: list[FeedItem] = []
    budget = page_size
    while phase < len(order) and budget > 0:
        entity_type = order[phase]
        if entity_type not in visible:
            phase, after = phase + 1, None
            continue
        rows = await projector.snapshot_page(
            ctx, scope, cursor.level, entity_type, after, budget + 1
        )
        more = len(rows) > budget
        taken = rows[:budget]
        for row in taken:
            items.append(
                FeedItem(
                    seq=None,
                    entity_type=entity_type,
                    entity_id=row.entity_id,
                    operation="upsert",
                    version=row.version,
                    changed_at=None,
                    data=row.data.model_dump(mode="json"),
                )
            )
        budget -= len(taken)
        if more:
            after = taken[-1].entity_id
            break
        phase, after = phase + 1, None
    if phase < len(order):
        next_cursor = cursor.continue_snapshot(phase, after)
        has_more = True
    else:
        # Snapshot complete: continue as a change feed from the captured head.
        next_cursor = cursor.at_changes(cursor.seq, None)
        has_more = head_seq > cursor.seq
    return ScopePage(scope, "ok", codec.encode(next_cursor), has_more, head_seq, tuple(items))


async def _changes_page(
    ctx: CommandContext,
    codec: CursorCodec,
    projector: Projector,
    cursor: Cursor,
    head_seq: int,
    page_size: int,
) -> ScopePage:
    scope = cursor.scope
    if page_size + 1 > READ_CHANGES_CAP:
        raise ValueError("page size exceeds the change-log read gate")
    watermark = cursor.watermark if cursor.watermark is not None else head_seq
    rows = (
        await ctx.session.execute(
            READ_CHANGES,
            {
                "scope_type": scope.scope_type.value,
                "scope_id": scope.scope_id,
                "after": cursor.seq,
                "upto": watermark,
                "limit": page_size + 1,
            },
        )
    ).all()
    # Compaction may have raised the floor past this cursor while the page was read;
    # rows it removed cannot be delivered, so the client must bootstrap again.
    floor_now = (
        await ctx.session.execute(
            READ_FLOOR, {"scope_type": scope.scope_type.value, "scope_id": scope.scope_id}
        )
    ).scalar_one_or_none()
    if floor_now is not None and cursor.seq < floor_now:
        return ScopePage(scope, "resync_required", head=head_seq)
    has_more = len(rows) > page_size
    rows = rows[:page_size]
    # Collapse repeated entities: the latest sequence decides the position and state.
    latest: dict[tuple[str, UUID], Any] = {}
    for row in rows:
        latest.pop((row.entity_type, row.entity_id), None)
        latest[(row.entity_type, row.entity_id)] = row
    visible = projector.visible_types(scope.scope_type.value, cursor.level)
    items: list[FeedItem] = []
    for (entity_type, entity_id), row in latest.items():
        if entity_type not in visible:
            continue
        data: dict[str, Any] | None = None
        operation: Literal["upsert", "delete"] = "delete"
        if row.operation != "delete":
            loaded = await projector.load(ctx, scope, cursor.level, entity_type, entity_id)
            if isinstance(loaded, Hidden):
                continue
            if loaded is not None:
                operation, data = "upsert", loaded.model_dump(mode="json")
        items.append(
            FeedItem(
                seq=row.scope_seq,
                entity_type=entity_type,
                entity_id=entity_id,
                operation=operation,
                version=row.entity_version,
                changed_at=row.changed_at,
                data=data,
            )
        )
    last_seq = rows[-1].scope_seq if has_more else watermark
    next_cursor = cursor.at_changes(last_seq, watermark if has_more else None)
    return ScopePage(scope, "ok", codec.encode(next_cursor), has_more, head_seq, tuple(items))
