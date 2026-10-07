"""Receipt archives: every receipt of a plan in one zip, for accounting.

Someone on an unlocked trip (or any hangout) asks for an archive; the worker packs the
ready receipts of the plan's active expenses into a zip on disk
(``BELUNO_RECEIPT_ARCHIVE_DIR``), one file at a time, with ``receipts.csv`` listing each
file's expense, uploads it, and queues its deletion a day later. Only the person who
asked can see the request and download the file. A ready archive is reused until new
receipts arrive; one in the making is shared; builds run one at a time; an hourly sweep
fails builds a crash left behind. No storage I/O happens inside a transaction.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import anyio
from sqlalchemy import Row, func, select, text, update
from sqlalchemy.exc import IntegrityError

from beluno.authorization.access import load_plan, require_plan
from beluno.authorization.policy import PlanAction
from beluno.contracts.common import spreadsheet_text
from beluno.contracts.errors import conflict, not_found
from beluno.db.ids import new_id
from beluno.db.models.finance import Currency, Expense
from beluno.db.models.media import Media, ReceiptArchive
from beluno.modules import billing
from beluno.modules.context import CommandContext, Runtime
from beluno.modules.media import MEDIA_QUEUE, storage_of
from beluno.modules.sync_audit.recorder import record_audit
from beluno.storage import ObjectStorage, ObjectTooLarge, archive_key, ready_key
from beluno.worker.enqueue import defer_in_transaction

BUILD_TASK = "media.build_receipt_archive"
KEEP_FOR = timedelta(hours=24)
# A build still unfinished this long after it was asked for died with its worker.
STALE_AFTER = timedelta(hours=1)
PENDING, BUILDING, READY, FAILED = "pending", "building", "ready", "failed"
IN_PROGRESS = (PENDING, BUILDING)
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
ENTRIES = text(
    "SELECT media_id, content_type, size_bytes, expense_id, occurred_on, description,"
    " amount_minor, currency FROM media_memories.archive_entries(:id)"
)
QUEUE_DELETION = text(
    "INSERT INTO media_memories.object_deletions (object_key, requested_at) "
    "VALUES (:key, :due) ON CONFLICT (object_key) DO NOTHING"
)
INDEX_COLUMNS = ("file", "date", "description", "amount", "currency", "expense_id")
logger = logging.getLogger("beluno.media")


@dataclass(frozen=True)
class ArchiveView:
    archive: ReceiptArchive
    state: str  # the stored state, or "expired" once the file is gone
    download_url: str | None


async def request_archive(ctx: CommandContext, plan_id: UUID) -> ArchiveView:
    """Ask for the plan's receipts in one zip, reusing what can be reused."""

    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW_FINANCE)
    await billing.require_unlocked(ctx, plan_id, access.plan.type, "The receipt archive")
    storage_of(ctx.runtime)  # 503 before anything is queued when storage is missing
    user_id = ctx.require_actor().user_id
    latest_receipt = await ctx.session.scalar(
        select(func.max(Media.updated_at)).where(
            Media.plan_id == plan_id,
            Media.kind == "receipt",
            Media.state == "ready",
            Media.deleted_at.is_(None),
            Media.expense_id.in_(
                select(Expense.id).where(Expense.plan_id == plan_id, Expense.state == "active")
            ),
        )
    )
    if latest_receipt is None:
        raise conflict("NO_RECEIPTS", "There are no receipts to archive yet")
    reusable = await _reusable(ctx, plan_id, user_id, latest_receipt)
    if reusable is not None:
        return await _view(ctx, reusable)
    archive = ReceiptArchive(
        id=new_id(),
        plan_id=plan_id,
        requested_by_user_id=user_id,
        state=PENDING,
        failure=None,
        receipts=None,
        size_bytes=None,
        created_at=ctx.now,
        ready_at=None,
        expires_at=None,
    )
    try:
        async with ctx.savepoint():
            ctx.session.add(archive)
            await ctx.session.flush()
    except IntegrityError:
        # Asked twice at the same moment, or a build the hourly sweep has yet to fail:
        # the archive already in the making is this one.
        found = (
            await ctx.session.execute(
                select(ReceiptArchive).where(
                    ReceiptArchive.plan_id == plan_id,
                    ReceiptArchive.requested_by_user_id == user_id,
                    ReceiptArchive.state.in_(IN_PROGRESS),
                )
            )
        ).scalar_one_or_none()
        if found is None:
            raise
        return await _view(ctx, found)
    await defer_in_transaction(
        ctx.session,
        task_name=BUILD_TASK,
        queue=MEDIA_QUEUE,
        args={"archive_id": str(archive.id)},
    )
    await record_audit(
        ctx,
        action="media.receipt_archive_requested",
        entity_type="receipt_archive",
        entity_id=archive.id,
        plan_id=plan_id,
    )
    return ArchiveView(archive, PENDING, None)


async def get_archive(ctx: CommandContext, plan_id: UUID, archive_id: UUID) -> ArchiveView:
    archive = await ctx.session.get(ReceiptArchive, archive_id)
    if archive is None or archive.plan_id != plan_id:
        raise not_found()  # someone else's archive is not found either (RLS)
    return await _view(ctx, archive)


async def _reusable(
    ctx: CommandContext, plan_id: UUID, user_id: UUID, latest_receipt: datetime
) -> ReceiptArchive | None:
    """One in the making, or a ready one built after the latest receipt arrived."""

    rows = (
        await ctx.session.execute(
            select(ReceiptArchive)
            .where(
                ReceiptArchive.plan_id == plan_id,
                ReceiptArchive.requested_by_user_id == user_id,
                (
                    ReceiptArchive.state.in_(IN_PROGRESS)
                    & (ReceiptArchive.created_at > ctx.now - STALE_AFTER)
                )
                | (
                    (ReceiptArchive.state == READY)
                    & (ReceiptArchive.expires_at > ctx.now)
                    & (ReceiptArchive.created_at >= latest_receipt)
                ),
            )
            .order_by(ReceiptArchive.created_at.desc())
            .limit(1)
        )
    ).scalars()
    return next(iter(rows), None)


async def _view(ctx: CommandContext, archive: ReceiptArchive) -> ArchiveView:
    if archive.state != READY:
        return ArchiveView(archive, archive.state, None)
    if archive.expires_at is not None and archive.expires_at <= ctx.now:
        return ArchiveView(archive, "expired", None)
    url = storage_of(ctx.runtime).download_url(
        archive_key(archive.id),
        "application/zip",
        f"beluno-receipts-{archive.plan_id}.zip",
        attachment=True,
    )
    return ArchiveView(archive, READY, url)


# --- worker --------------------------------------------------------------------------


async def build_archive(runtime: Runtime, archive_id: UUID) -> str:
    """Pack the receipts, upload the zip, and queue its deletion; returns the state."""

    async with runtime.database.transaction() as session:
        archive = (
            await session.execute(
                select(ReceiptArchive)
                .where(ReceiptArchive.id == archive_id, ReceiptArchive.state == PENDING)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if archive is None:
            return "skipped"  # built already, being built, or failed by the sweep
        archive.state = BUILDING
        entries = list((await session.execute(ENTRIES, {"id": archive_id})).all())
        exponents = dict((await session.execute(select(Currency.code, Currency.exponent))).all())
    settings = runtime.settings
    # Sizes are known before anything is read: a too-large archive fails at once.
    if sum(entry.size_bytes or 0 for entry in entries) > settings.receipt_archive_max_bytes:
        return await _finish(runtime, archive_id, FAILED, failure="too_large")
    try:
        storage = storage_of(runtime)
        with tempfile.TemporaryDirectory(
            prefix="beluno-archive-", dir=settings.receipt_archive_dir
        ) as folder:
            path = Path(folder) / "receipts.zip"
            packed = await _pack(
                storage, entries, exponents, path, settings.media_receipt_max_bytes
            )
            await storage.write_file(archive_key(archive_id), path, "application/zip")
            size = path.stat().st_size
    except Exception:
        logger.exception("could not build a receipt archive")
        return await _finish(runtime, archive_id, FAILED, failure="storage")
    return await _finish(runtime, archive_id, READY, receipts=packed, size=size)


async def fail_stale_archives(runtime: Runtime) -> int:
    """Hourly: builds a crash or a deploy cut short fail, and free the person to ask again."""

    now = runtime.clock()
    async with runtime.database.transaction() as session:
        stale = list(
            (
                await session.execute(
                    select(ReceiptArchive.id).where(
                        ReceiptArchive.state.in_(IN_PROGRESS),
                        ReceiptArchive.created_at < now - STALE_AFTER,
                    )
                )
            ).scalars()
        )
        if stale:
            await session.execute(
                update(ReceiptArchive)
                .where(ReceiptArchive.id.in_(stale), ReceiptArchive.state.in_(IN_PROGRESS))
                .values(state=FAILED, failure="storage")
            )
            for archive_id in stale:
                # A half-uploaded zip, if any, goes too.
                await session.execute(QUEUE_DELETION, {"key": archive_key(archive_id), "due": now})
    return len(stale)


async def _pack(
    storage: ObjectStorage,
    entries: list[Row[Any]],
    exponents: dict[str, int],
    path: Path,
    max_receipt_bytes: int,
) -> int:
    """Write the zip one receipt at a time; returns how many receipts it holds."""

    index = io.StringIO()
    writer = csv.writer(index)
    writer.writerow(INDEX_COLUMNS)
    packed = 0
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zip_:
        for number, entry in enumerate(entries, start=1):
            try:
                data = await storage.read(ready_key(entry.media_id), max_receipt_bytes)
            except ObjectTooLarge:
                data = None
            name = (
                f"{entry.occurred_on.isoformat()}_{number:04d}_{_slug(entry.description)}."
                f"{EXTENSIONS.get(entry.content_type, 'pdf')}"
            )
            if data is not None:
                # Writing and checksumming up to a few megabytes: off the event loop.
                await anyio.to_thread.run_sync(zip_.writestr, name, data)
                packed += 1
            writer.writerow(
                [
                    name if data is not None else "(missing)",
                    entry.occurred_on.isoformat(),
                    spreadsheet_text(entry.description),
                    str(Decimal(entry.amount_minor).scaleb(-exponents.get(entry.currency, 2))),
                    entry.currency,
                    str(entry.expense_id),
                ]
            )
        zip_.writestr("receipts.csv", index.getvalue().encode("utf-8-sig"))
    return packed


async def _finish(
    runtime: Runtime,
    archive_id: UUID,
    state: str,
    *,
    failure: str | None = None,
    receipts: int | None = None,
    size: int | None = None,
) -> str:
    now = runtime.clock()
    async with runtime.database.transaction() as session:
        values: dict[str, object] = {"state": state, "failure": failure}
        if state == READY:
            values |= {
                "receipts": receipts,
                "size_bytes": size,
                "ready_at": now,
                "expires_at": now + KEEP_FOR,
            }
            # The file goes a day later, whatever happens to the request.
            await session.execute(
                QUEUE_DELETION, {"key": archive_key(archive_id), "due": now + KEEP_FOR}
            )
        await session.execute(
            update(ReceiptArchive)
            .where(ReceiptArchive.id == archive_id, ReceiptArchive.state == BUILDING)
            .values(**values)
        )
    return state


def _slug(description: str) -> str:
    """A file-name-safe short form of the description (letters of any script kept)."""

    cleaned = re.sub(r"[^\w\-]+", "-", description, flags=re.UNICODE).strip("-_")
    return cleaned[:40] or "receipt"
