"""Trip tasks: who does what, by when, and whether it is done.

One assignee per task (a participant row merged into another counts as that
one). The creator or an organiser edits it, keeping the status unless the edit
names one; the assignee, the creator, or an organiser moves its status
(``set_status`` is an intent command, so an offline device's "done" lands
without a version check). ``remind_at`` is only recorded here; delivering
reminders comes with notifications.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from beluno.authorization.access import PlanAccess
from beluno.authorization.policy import AccessState, PlanAction
from beluno.contracts.errors import conflict, not_found, validation_error, version_conflict
from beluno.db.ids import new_id
from beluno.db.models.bookings import Booking
from beluno.db.models.coordination import Task
from beluno.db.models.plans import PlanParticipant
from beluno.db.models.schedule_places import ItineraryItem
from beluno.modules.activity.events import ActivityItem, ActivityType, item
from beluno.modules.context import CommandContext
from beluno.modules.planning.common import (
    planning_access,
    record_planning_change,
    require_author_or_manager,
)
from beluno.modules.plans.timing import resolve_local

TASK_ENTITY = "task"
OPEN, IN_PROGRESS, DONE = "open", "in_progress", "done"


@dataclass(frozen=True)
class TaskDraft:
    title: str
    note: str | None = None
    assignee_participant_id: UUID | None = None
    due_date: date | None = None
    due_time: time | None = None
    due_timezone: str | None = None
    remind_at: datetime | None = None
    status: str | None = None  # None keeps the current status (open for a new task)
    item_id: UUID | None = None
    booking_id: UUID | None = None


async def list_tasks(ctx: CommandContext, plan_id: UUID) -> list[Task]:
    await planning_access(ctx, plan_id, PlanAction.VIEW)
    rows = await ctx.session.execute(
        select(Task)
        .where(Task.plan_id == plan_id, Task.deleted_at.is_(None))
        .order_by(Task.due_date.asc().nulls_last(), Task.id)
    )
    return list(rows.scalars())


async def get_task(ctx: CommandContext, plan_id: UUID, task_id: UUID) -> Task:
    await planning_access(ctx, plan_id, PlanAction.VIEW)
    return await _find(ctx, plan_id, task_id)


async def create_task(
    ctx: CommandContext, plan_id: UUID, task_id: UUID | None, draft: TaskDraft
) -> Task:
    await planning_access(ctx, plan_id, PlanAction.CONTRIBUTE_PLANNING)
    task = Task(
        id=task_id or new_id(),
        plan_id=plan_id,
        completed_at=None,
        completed_by_user_id=None,
        created_by_user_id=ctx.require_actor().user_id,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
        deleted_at=None,
    )
    await _apply(ctx, task, draft)
    try:
        async with ctx.savepoint():
            ctx.session.add(task)
            await ctx.session.flush()
    except IntegrityError as error:
        raise conflict("ALREADY_EXISTS", "A resource with this id already exists") from error
    await _record(ctx, task, "planning.task_added", _completed(task, was_done=False))
    return task


async def update_task(
    ctx: CommandContext, plan_id: UUID, task_id: UUID, expected_version: int, draft: TaskDraft
) -> Task:
    access = await planning_access(ctx, plan_id, PlanAction.VIEW)
    task = await _find(ctx, plan_id, task_id, for_update=True)
    await require_author_or_manager(ctx, access, task.created_by_user_id)
    if task.version != expected_version:
        raise version_conflict(task)
    was_done = task.status == DONE
    await _apply(ctx, task, draft)
    await _bump(ctx, task, "planning.task_updated", _completed(task, was_done))
    return task


async def set_status(ctx: CommandContext, plan_id: UUID, task_id: UUID, status: str) -> Task:
    """The assignee, the creator, or an organiser moves the task along."""

    access = await planning_access(ctx, plan_id, PlanAction.RESPOND_PLANNING)
    task = await _find(ctx, plan_id, task_id, for_update=True)
    if not await _is_assignee(ctx, access, task):
        await require_author_or_manager(ctx, access, task.created_by_user_id)
    if task.status == status:
        return task
    was_done = task.status == DONE
    _set_status(ctx, task, status)
    await _bump(ctx, task, "planning.task_status_changed", _completed(task, was_done))
    return task


async def delete_task(ctx: CommandContext, plan_id: UUID, task_id: UUID) -> None:
    access = await planning_access(ctx, plan_id, PlanAction.VIEW)
    task = await _find(ctx, plan_id, task_id, for_update=True)
    await require_author_or_manager(ctx, access, task.created_by_user_id)
    task.deleted_at = ctx.now
    await _bump(ctx, task, "planning.task_deleted", operation="delete")


async def _is_assignee(ctx: CommandContext, access: PlanAccess, task: Task) -> bool:
    """The caller's row is the assignee, or the assignee row was merged into it."""

    own = access.participant
    if own is None or task.assignee_participant_id is None:
        return False
    if own.id == task.assignee_participant_id:
        return True
    merged = await ctx.session.scalar(
        select(PlanParticipant.id).where(
            PlanParticipant.plan_id == task.plan_id,
            PlanParticipant.id == task.assignee_participant_id,
            PlanParticipant.merged_into_participant_id == own.id,
        )
    )
    return merged is not None


def _completed(task: Task, was_done: bool) -> ActivityItem | None:
    return item(ActivityType.TASK_COMPLETED) if task.status == DONE and not was_done else None


def _set_status(ctx: CommandContext, task: Task, status: str) -> None:
    task.status = status
    if status == DONE:
        task.completed_at = task.completed_at or ctx.now
        task.completed_by_user_id = task.completed_by_user_id or ctx.require_actor().user_id
    else:
        task.completed_at, task.completed_by_user_id = None, None


async def _apply(ctx: CommandContext, task: Task, draft: TaskDraft) -> None:
    if draft.due_time is not None:
        assert draft.due_date is not None and draft.due_timezone is not None
        resolve_local(datetime.combine(draft.due_date, draft.due_time), draft.due_timezone)
    plan_id = task.plan_id
    if (
        draft.assignee_participant_id is not None
        and draft.assignee_participant_id != task.assignee_participant_id
    ):
        found = await ctx.session.scalar(
            select(PlanParticipant.id).where(
                PlanParticipant.plan_id == plan_id,
                PlanParticipant.id == draft.assignee_participant_id,
                PlanParticipant.access_state == AccessState.ACTIVE.value,
            )
        )
        if found is None:
            raise validation_error("assignee_participant_id is not an active participant")
    # Unchanged links stay valid even after the item or booking was deleted.
    if draft.item_id is not None and draft.item_id != task.item_id:
        found = await ctx.session.scalar(
            select(ItineraryItem.id).where(
                ItineraryItem.plan_id == plan_id,
                ItineraryItem.id == draft.item_id,
                ItineraryItem.deleted_at.is_(None),
            )
        )
        if found is None:
            raise validation_error("item_id does not name an itinerary item of this plan")
    if draft.booking_id is not None and draft.booking_id != task.booking_id:
        found = await ctx.session.scalar(
            select(Booking.id).where(
                Booking.plan_id == plan_id,
                Booking.id == draft.booking_id,
                Booking.deleted_at.is_(None),
            )
        )
        if found is None:
            raise validation_error("booking_id does not name a booking of this plan")
    task.title = draft.title
    task.note = draft.note
    task.assignee_participant_id = draft.assignee_participant_id
    task.due_date, task.due_time = draft.due_date, draft.due_time
    task.due_timezone = draft.due_timezone
    task.remind_at = draft.remind_at
    task.item_id, task.booking_id = draft.item_id, draft.booking_id
    _set_status(ctx, task, draft.status or task.status or OPEN)


async def _find(
    ctx: CommandContext, plan_id: UUID, task_id: UUID, *, for_update: bool = False
) -> Task:
    statement = select(Task).where(Task.plan_id == plan_id, Task.id == task_id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    task = (await ctx.session.execute(statement)).scalar_one_or_none()
    if task is None or task.deleted_at is not None:
        raise not_found()
    return task


async def _bump(
    ctx: CommandContext,
    task: Task,
    action: str,
    activity: ActivityItem | None = None,
    *,
    operation: str = "upsert",
) -> None:
    task.version += 1
    task.updated_at = ctx.now
    await ctx.session.flush()
    await _record(ctx, task, action, activity, operation=operation)


async def _record(
    ctx: CommandContext,
    task: Task,
    action: str,
    activity: ActivityItem | None = None,
    *,
    operation: str = "upsert",
) -> None:
    await record_planning_change(
        ctx,
        action=action,
        entity_type=TASK_ENTITY,
        entity_id=task.id,
        entity_version=task.version,
        plan_id=task.plan_id,
        metadata={"version": task.version, "status": task.status},
        operation=operation,
        activity=activity,
    )
