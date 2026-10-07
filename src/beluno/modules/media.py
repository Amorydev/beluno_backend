"""A plan's uploaded files: receipts on expenses, trip covers (and memories later).

The bytes never pass through the API:

1. ``create_media`` records the file (offline-capable: a sync command), awaiting upload.
2. ``upload_url`` signs a PUT for exactly the declared type and size, into
   ``incoming/{id}``; the app uploads straight to storage.
3. ``mark_uploaded`` moves it to scanning and queues ``process_media``.
4. The worker reads the object, checks its real type and size, streams it to
   ClamAV, rewrites images without metadata, stores the result as ``media/{id}``,
   deletes the incoming copy, and settles the row as ready or rejected.
5. ``download_url`` signs a short GET for anyone who can see the plan.

Deleting a file (or purging its plan) queues its objects; ``delete_queued_objects``
removes them from storage. No storage call happens inside a database transaction.
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import anyio
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from beluno.authorization.access import PlanAccess, load_plan, require_plan
from beluno.authorization.policy import PLAN_MANAGERS, PlanAction
from beluno.contracts.errors import (
    BelunoError,
    conflict,
    forbidden,
    invalid_state,
    not_found,
    validation_error,
)
from beluno.db.ids import new_id
from beluno.db.models.finance import Expense
from beluno.db.models.media import Media, ObjectDeletion
from beluno.media_files import IMAGE_TYPES, PDF, CleanFile, RejectedFile, clean
from beluno.modules.context import CommandContext, Runtime, open_context
from beluno.modules.plans.changes import record_plan_change
from beluno.modules.sync_audit.recorder import ChangeScope, record_mutation
from beluno.storage import (
    DOWNLOAD_URL_TTL,
    UPLOAD_URL_TTL,
    ObjectStorage,
    ObjectTooLarge,
    StorageNotConfigured,
    incoming_key,
    ready_key,
)
from beluno.worker.enqueue import defer_in_transaction

MEDIA_ENTITY = "media"
RECEIPT, COVER, MEMORY = "receipt", "cover", "memory"
AWAITING, SCANNING, READY, REJECTED = "awaiting_upload", "scanning", "ready", "rejected"
PROCESS_TASK = "media.process"
MEDIA_QUEUE = "media"  # its own queue: scanning never delays sign-in email
STUCK_AFTER = timedelta(hours=1)
ABANDONED_AFTER = timedelta(days=7)
QUEUE_KEY = text(
    "INSERT INTO media_memories.object_deletions (object_key, requested_at) "
    "VALUES (:key, :due) ON CONFLICT (object_key) DO NOTHING"
)
DELETE_BATCH = 200
logger = logging.getLogger("beluno.media")
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", PDF: "pdf"}


@dataclass(frozen=True)
class SignedUrl:
    url: str
    expires_at: datetime


async def create_media(
    ctx: CommandContext,
    plan_id: UUID,
    media_id: UUID | None,
    *,
    kind: str,
    declared_type: str,
    declared_size: int,
    expense_id: UUID | None = None,
) -> Media:
    access = await _plan(ctx, plan_id, kind)
    if declared_type == PDF and kind != RECEIPT:
        raise validation_error("only receipts may be PDF files")
    if declared_type != PDF and declared_type not in IMAGE_TYPES:
        raise validation_error(
            "the file must be a JPEG, PNG, WebP, or HEIC image (or a PDF receipt)"
        )
    if declared_size > _max_bytes(ctx, kind):
        raise validation_error(f"the file must be at most {_max_bytes(ctx, kind)} bytes")
    if kind == RECEIPT:
        await _require_expense(ctx, access, expense_id)
        await _check_receipt_quota(ctx, plan_id)
    elif expense_id is not None:
        raise validation_error("only receipts belong to an expense")
    media = Media(
        id=media_id or new_id(),
        plan_id=plan_id,
        kind=kind,
        state=AWAITING,
        rejection=None,
        declared_type=declared_type,
        declared_size=declared_size,
        content_type=None,
        size_bytes=None,
        width=None,
        height=None,
        expense_id=expense_id if kind == RECEIPT else None,
        uploaded_by_user_id=ctx.require_actor().user_id,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
        deleted_at=None,
    )
    try:
        async with ctx.savepoint():
            ctx.session.add(media)
            await ctx.session.flush()
    except IntegrityError as error:
        raise conflict("ALREADY_EXISTS", "A resource with this id already exists") from error
    await _record(ctx, media, "media.added")
    return media


async def upload_url(ctx: CommandContext, plan_id: UUID, media_id: UUID) -> SignedUrl:
    media = await _own_upload(ctx, plan_id, media_id)
    url = _storage(ctx.runtime).upload_url(
        incoming_key(media.id), media.declared_type, media.declared_size
    )
    return SignedUrl(url, ctx.now + UPLOAD_URL_TTL)


async def mark_uploaded(ctx: CommandContext, plan_id: UUID, media_id: UUID) -> Media:
    """The upload finished: scan it. Asking again while it is scanning changes nothing."""

    media = await _find(ctx, plan_id, media_id, for_update=True)
    if media.uploaded_by_user_id != ctx.require_actor().user_id:
        raise forbidden("Only the person uploading the file reports it uploaded")
    if media.state == SCANNING:
        return media
    if media.state != AWAITING:
        raise invalid_state("the file was already processed")
    media.state = SCANNING
    await _bump(ctx, media, "media.uploaded")
    await defer_in_transaction(
        ctx.session,
        task_name=PROCESS_TASK,
        queue=MEDIA_QUEUE,
        args={"media_id": str(media.id)},
        queueing_lock=f"media:{media.id}",
    )
    return media


async def download_url(ctx: CommandContext, plan_id: UUID, media_id: UUID) -> SignedUrl:
    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW)
    media = await _find(ctx, plan_id, media_id)
    if media.state != READY or media.content_type is None:
        raise invalid_state("the file is not ready")
    extension = EXTENSIONS.get(media.content_type, "bin")
    url = _storage(ctx.runtime).download_url(
        ready_key(media.id),
        media.content_type,
        f"{media.kind}-{media.id}.{extension}",
        # PDFs are kept as uploaded: save them rather than open them in the browser.
        attachment=media.content_type == PDF,
    )
    return SignedUrl(url, ctx.now + DOWNLOAD_URL_TTL)


async def delete_media(ctx: CommandContext, plan_id: UUID, media_id: UUID) -> None:
    """The uploader or an organiser deletes a file; the database queues its objects."""

    # The plan first (then the file), as setting a cover locks them: no cover can
    # point at a file deleted in between.
    access = await load_plan(ctx, plan_id, for_update=True)
    require_plan(access, PlanAction.VIEW)
    media = await _find(ctx, plan_id, media_id, for_update=True)
    actor = ctx.require_actor().user_id
    if media.uploaded_by_user_id != actor and access.role not in PLAN_MANAGERS:
        raise forbidden("Only the uploader or an organiser deletes this file")
    plan = access.plan
    if plan.cover_media_id == media.id:
        require_plan(access, PlanAction.UPDATE)
        plan.cover_media_id = None
        plan.version += 1
        plan.updated_at = ctx.now
        await ctx.session.flush()
        await record_plan_change(ctx, plan, "plan.cover_removed")
    media.deleted_at = ctx.now
    await _bump(ctx, media, "media.deleted", operation="delete")


async def media_of_plan(ctx: CommandContext, plan_id: UUID) -> list[Media]:
    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW)
    rows = await ctx.session.execute(
        select(Media).where(Media.plan_id == plan_id, Media.deleted_at.is_(None)).order_by(Media.id)
    )
    return list(rows.scalars())


async def ready_cover(ctx: CommandContext, plan_id: UUID, media_id: UUID) -> Media:
    """A ready cover file of this plan (for setting the plan's cover)."""

    media = (
        await ctx.session.execute(select(Media).where(Media.id == media_id).with_for_update())
    ).scalar_one_or_none()
    if (
        media is None
        or media.plan_id != plan_id
        or media.deleted_at is not None
        or media.kind != COVER
        or media.state != READY
    ):
        raise validation_error("cover_media_id must name a ready cover of this plan")
    return media


# --- worker ------------------------------------------------------------------------------


async def process_media(runtime: Runtime, media_id: UUID) -> str:
    """Scan and clean one uploaded file; returns its final state.

    Storage and the scanner are reached outside any transaction. A scanner or storage
    that is down raises, so the job retries; anything about the file itself settles
    it. Objects nothing points at any more are queued for deletion, and the incoming
    copy goes only after the result is committed.
    """

    async with runtime.database.transaction() as session:
        media = await session.get(Media, media_id)
        if media is None or media.deleted_at is not None or media.state != SCANNING:
            return media.state if media is not None else "missing"
        kind, declared_size = media.kind, media.declared_size
    storage = _storage(runtime)
    outcome, cleaned = await _inspect(runtime, storage, media_id, kind, declared_size)
    if cleaned is not None:
        await storage.write(ready_key(media_id), cleaned.data, cleaned.content_type)
    async with open_context(runtime) as ctx:
        media = (
            await ctx.session.execute(select(Media).where(Media.id == media_id).with_for_update())
        ).scalar_one_or_none()
        settled = media is not None and media.deleted_at is None and media.state == SCANNING
        if media is not None and settled:
            media.state, media.rejection = outcome
            if cleaned is not None:
                media.content_type = cleaned.content_type
                media.size_bytes = len(cleaned.data)
                media.width, media.height = cleaned.width, cleaned.height
            await _bump(ctx, media, "media.ready" if cleaned else "media.rejected")
        elif cleaned is not None:
            # Deleted or purged meanwhile: the copy just stored belongs to nothing.
            await _queue(ctx, ready_key(media_id), ctx.now)
        # Deleted below; queued again for when a still-valid upload URL is reused.
        await _queue(ctx, incoming_key(media_id), ctx.now + UPLOAD_URL_TTL)
        state = media.state if media is not None else "missing"
    await storage.delete(incoming_key(media_id))
    return state


async def sweep_media(runtime: Runtime) -> int:
    """Hourly: re-queue files stuck scanning, and drop uploads abandoned for a week."""

    async with open_context(runtime) as ctx:
        stuck = list(
            (
                await ctx.session.execute(
                    select(Media.id).where(
                        Media.state == SCANNING,
                        Media.deleted_at.is_(None),
                        Media.updated_at < ctx.now - STUCK_AFTER,
                    )
                )
            ).scalars()
        )
        for media_id in stuck:
            await defer_in_transaction(
                ctx.session,
                task_name=PROCESS_TASK,
                queue=MEDIA_QUEUE,
                args={"media_id": str(media_id)},
                queueing_lock=f"media:{media_id}",
            )
        abandoned = list(
            (
                await ctx.session.execute(
                    select(Media.id).where(
                        Media.state == AWAITING,
                        Media.deleted_at.is_(None),
                        Media.updated_at < ctx.now - ABANDONED_AFTER,
                    )
                )
            ).scalars()
        )
        for media_id in abandoned:
            await _queue(ctx, incoming_key(media_id), ctx.now)
    return len(stuck) + len(abandoned)


async def delete_queued_objects(runtime: Runtime) -> int:
    """Remove due objects from storage, a batch at a time; a failing key waits its turn."""

    now = runtime.clock()
    async with runtime.database.transaction() as session:
        keys = list(
            (
                await session.execute(
                    select(ObjectDeletion.object_key)
                    .where(ObjectDeletion.requested_at <= now)
                    .order_by(ObjectDeletion.requested_at, ObjectDeletion.object_key)
                    .limit(DELETE_BATCH)
                )
            ).scalars()
        )
    storage = _storage(runtime)
    removed: list[str] = []
    for key in keys:
        try:
            await storage.delete(key)
        except Exception:
            logger.warning("could not delete a media object; it stays queued")
            continue
        removed.append(key)
    if removed:
        async with runtime.database.transaction() as session:
            await session.execute(
                delete(ObjectDeletion).where(ObjectDeletion.object_key.in_(removed))
            )
    return len(removed)


async def _inspect(
    runtime: Runtime, storage: ObjectStorage, media_id: UUID, kind: str, declared_size: int
) -> tuple[tuple[str, str | None], CleanFile | None]:
    try:
        data = await storage.read(
            incoming_key(media_id), min(declared_size, _max_bytes_for(runtime, kind))
        )
    except ObjectTooLarge:
        return (REJECTED, "size"), None
    if data is None:
        return (REJECTED, "missing"), None
    if len(data) != declared_size:
        return (REJECTED, "size"), None
    verdict = await runtime.scanner.scan(data)
    if verdict.too_large:
        return (REJECTED, "size"), None
    if not verdict.clean:
        return (REJECTED, "malware"), None
    try:
        # Decoding and encoding images is CPU work: off the event loop.
        cleaned = await anyio.to_thread.run_sync(
            functools.partial(clean, data, allow_pdf=kind == RECEIPT)
        )
    except RejectedFile as error:
        return (REJECTED, error.reason), None
    return (READY, None), cleaned


async def _queue(ctx: CommandContext, key: str, due: datetime) -> None:
    await ctx.session.execute(QUEUE_KEY, {"key": key, "due": due})


def _storage(runtime: Runtime) -> ObjectStorage:
    try:
        return runtime.storage
    except StorageNotConfigured as error:
        raise BelunoError(
            status=503, code="MEDIA_UNAVAILABLE", title="File storage is not available"
        ) from error


# --- helpers -----------------------------------------------------------------------------


async def _plan(ctx: CommandContext, plan_id: UUID, kind: str) -> PlanAccess:
    access = await load_plan(ctx, plan_id)
    if kind == RECEIPT:
        require_plan(access, PlanAction.CREATE_EXPENSE)
    elif kind == COVER:
        require_plan(access, PlanAction.UPDATE)
        if access.plan.type != "trip":
            raise conflict("NOT_AVAILABLE_FOR_HANGOUT", "Covers are for trips")
    else:
        raise validation_error("kind must be receipt or cover")
    return access


async def _require_expense(
    ctx: CommandContext, access: PlanAccess, expense_id: UUID | None
) -> None:
    if expense_id is None:
        raise validation_error("a receipt needs its expense_id")
    found = await ctx.session.scalar(
        select(Expense.id).where(Expense.plan_id == access.plan.id, Expense.id == expense_id)
    )
    if found is None:
        raise validation_error("expense_id does not name an expense of this plan")


async def _check_receipt_quota(ctx: CommandContext, plan_id: UUID) -> None:
    limit = ctx.settings.media_receipts_per_plan
    if limit is None:
        return
    count = await ctx.session.scalar(
        select(func.count())
        .select_from(Media)
        .where(Media.plan_id == plan_id, Media.kind == RECEIPT, Media.deleted_at.is_(None))
    )
    if int(count or 0) >= limit:
        raise conflict("MEDIA_LIMIT_REACHED", f"This plan already has {limit} receipts")


def _max_bytes(ctx: CommandContext, kind: str) -> int:
    return _max_bytes_for(ctx.runtime, kind)


def _max_bytes_for(runtime: Runtime, kind: str) -> int:
    settings = runtime.settings
    return settings.media_receipt_max_bytes if kind == RECEIPT else settings.media_image_max_bytes


async def _own_upload(ctx: CommandContext, plan_id: UUID, media_id: UUID) -> Media:
    media = await _find(ctx, plan_id, media_id)
    if media.uploaded_by_user_id != ctx.require_actor().user_id:
        raise forbidden("Only the person uploading the file gets its upload address")
    if media.state != AWAITING:
        raise invalid_state("the file was already uploaded")
    return media


async def _find(
    ctx: CommandContext, plan_id: UUID, media_id: UUID, *, for_update: bool = False
) -> Media:
    statement = select(Media).where(Media.plan_id == plan_id, Media.id == media_id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    media = (await ctx.session.execute(statement)).scalar_one_or_none()
    if media is None or media.deleted_at is not None:
        raise not_found()
    return media


async def _bump(
    ctx: CommandContext, media: Media, action: str, *, operation: str = "upsert"
) -> None:
    media.version += 1
    media.updated_at = ctx.now
    await ctx.session.flush()
    await _record(ctx, media, action, operation=operation)


async def _record(
    ctx: CommandContext, media: Media, action: str, *, operation: str = "upsert"
) -> None:
    await record_mutation(
        ctx,
        action=action,
        entity_type=MEDIA_ENTITY,
        entity_id=media.id,
        entity_version=media.version,
        scope=ChangeScope.PLAN,
        scope_id=media.plan_id,
        plan_id=media.plan_id,
        metadata={"kind": media.kind, "state": media.state},
        operation=operation,
    )
