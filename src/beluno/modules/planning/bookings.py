"""Bookings: what the trip reserved, what it costs, and its secrets kept sealed.

A booking's price is a finance commitment (``booking``, ``price``): estimated
while planned, committed once confirmed, cancelled with the booking unless an
expense already paid it (then the expense stays). "Create expense from booking"
is an ordinary expense naming the booking's ``commitment_id``.

The confirmation code and private notes are sealed with ``beluno.secret_box``
into ``bookings.booking_secrets``, which only the booking's travelers, its
creator, and organisers can read (RLS). Responses and sync carry only whether
each is set; ``reveal`` returns the plaintext, audited and rate-limited, and is
not a command, so no replayable response ever stores it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from functools import lru_cache
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from beluno.authorization.access import PlanAccess
from beluno.authorization.policy import AccessState, PlanAction
from beluno.contracts.errors import (
    BelunoError,
    conflict,
    feature_disabled,
    forbidden,
    not_found,
    validation_error,
    version_conflict,
)
from beluno.db.ids import new_id
from beluno.db.models.bookings import Booking, BookingSecret
from beluno.db.models.plans import PlanParticipant
from beluno.db.models.schedule_places import Place
from beluno.modules.activity.events import ActivityItem, ActivityType, item
from beluno.modules.context import CommandContext
from beluno.modules.finance.states import CommitmentState
from beluno.modules.planning import costs
from beluno.modules.planning.common import (
    planning_access,
    record_planning_change,
    require_author_or_manager,
)
from beluno.modules.plans.timing import resolve_local
from beluno.modules.sync_audit.recorder import record_audit
from beluno.secret_box import Sealed, SecretBox, SecretBoxError

BOOKING_ENTITY = "booking"
COST_SOURCE, COST_KIND = "booking", "price"
PLANNED, CONFIRMED, CANCELLED = "planned", "confirmed", "cancelled"
CODE, NOTES = "confirmation_code", "private_notes"
MAY_REVEAL_SQL = text("SELECT bookings.actor_may_reveal(:booking_id)")
# Which budget category a booking's price lands in when none is given.
KIND_CATEGORY = {
    "flight": "transport",
    "transport": "transport",
    "lodging": "lodging",
    "activity": "activities",
    "restaurant": "food",
    "insurance": "fees",
    "other": "other",
}


class _Keep:
    """A secret left out of the request: what is stored stays."""


KEEP = _Keep()


@dataclass(frozen=True)
class Secrets:
    """The secrets to change: a value sets one, None clears it, ``KEEP`` leaves it."""

    confirmation_code: str | _Keep | None = KEEP
    private_notes: str | _Keep | None = KEEP


@dataclass(frozen=True)
class BookingDraft:
    kind: str
    title: str
    provider: str | None = None
    start_date: date | None = None
    start_time: time | None = None
    start_timezone: str | None = None
    end_date: date | None = None
    end_time: time | None = None
    end_timezone: str | None = None
    place_id: UUID | None = None
    traveler_ids: tuple[UUID, ...] = ()
    status: str = PLANNED
    payment_note: str | None = None
    free_cancellation_until: datetime | None = None
    price: costs.PlannedCost | None = None
    secrets: Secrets | None = None  # None keeps what is stored


@dataclass(frozen=True)
class BookingView:
    booking: Booking
    commitment_id: UUID | None


@dataclass(frozen=True)
class Revealed:
    confirmation_code: str | None
    private_notes: str | None


@lru_cache(maxsize=4)
def _box(keyring: str) -> SecretBox:
    return SecretBox.from_json(keyring)


def secret_box(ctx: CommandContext) -> SecretBox:
    if ctx.settings.booking_keys is None:
        raise feature_disabled()
    return _box(ctx.settings.booking_keys.get_secret_value())


async def list_bookings(ctx: CommandContext, plan_id: UUID) -> list[BookingView]:
    await planning_access(ctx, plan_id, PlanAction.VIEW)
    rows = await ctx.session.execute(
        select(Booking)
        .where(Booking.plan_id == plan_id, Booking.deleted_at.is_(None))
        .order_by(Booking.start_date.asc().nulls_last(), Booking.id)
    )
    return await booking_views(ctx, list(rows.scalars()))


async def get_booking(ctx: CommandContext, plan_id: UUID, booking_id: UUID) -> BookingView:
    await planning_access(ctx, plan_id, PlanAction.VIEW)
    return (await booking_views(ctx, [await _find(ctx, plan_id, booking_id)]))[0]


async def create_booking(
    ctx: CommandContext, plan_id: UUID, booking_id: UUID | None, draft: BookingDraft
) -> BookingView:
    access = await planning_access(ctx, plan_id, PlanAction.CONTRIBUTE_PLANNING, for_update=True)
    booking = Booking(
        id=booking_id or new_id(),
        plan_id=plan_id,
        has_confirmation_code=False,
        has_private_notes=False,
        created_by_user_id=ctx.require_actor().user_id,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
        deleted_at=None,
    )
    await _apply(ctx, access, booking, draft)
    _mark_secrets(booking, draft.secrets)
    try:
        async with ctx.savepoint():
            ctx.session.add(booking)
            await ctx.session.flush()
    except IntegrityError as error:
        raise conflict("ALREADY_EXISTS", "A resource with this id already exists") from error
    if draft.secrets is not None:
        await _seal(ctx, booking, draft.secrets)
        await ctx.session.flush()
    commitment_id = await _record_cost(ctx, access, booking, draft.price)
    await _record(
        ctx,
        booking,
        "planning.booking_added",
        item(ActivityType.BOOKING_ADDED, booking_kind=booking.kind),
    )
    return BookingView(booking, commitment_id)


async def update_booking(
    ctx: CommandContext,
    plan_id: UUID,
    booking_id: UUID,
    expected_version: int,
    draft: BookingDraft,
) -> BookingView:
    access = await planning_access(ctx, plan_id, PlanAction.VIEW, for_update=True)
    booking = await _find(ctx, plan_id, booking_id, for_update=True)
    await require_author_or_manager(ctx, access, booking.created_by_user_id)
    if booking.version != expected_version:
        raise version_conflict(booking)
    previous = booking.status
    await _apply(ctx, access, booking, draft)
    _mark_secrets(booking, draft.secrets)
    # The booking's own write first, with its next version: reading the secrets row
    # and the finance port both flush the session, and the write guard wants both.
    booking.version += 1
    booking.updated_at = ctx.now
    await ctx.session.flush()
    if draft.secrets is not None:
        await _seal(ctx, booking, draft.secrets)
        await ctx.session.flush()
    await _record_cost(ctx, access, booking, draft.price)
    activity = None
    if booking.status != previous and booking.status == CONFIRMED:
        activity = item(ActivityType.BOOKING_CONFIRMED, booking_kind=booking.kind)
    elif booking.status != previous and booking.status == CANCELLED:
        activity = item(ActivityType.BOOKING_CANCELLED, booking_kind=booking.kind)
    await _record(
        ctx,
        booking,
        "planning.booking_updated",
        activity,
        secrets_changed=draft.secrets is not None,
    )
    return (await booking_views(ctx, [booking]))[0]


async def delete_booking(ctx: CommandContext, plan_id: UUID, booking_id: UUID) -> None:
    access = await planning_access(ctx, plan_id, PlanAction.VIEW, for_update=True)
    booking = await _find(ctx, plan_id, booking_id, for_update=True)
    await require_author_or_manager(ctx, access, booking.created_by_user_id)
    await _record_cost(ctx, access, booking, None)
    # A deleted booking keeps no secrets (and no row pins an old key).
    row = await ctx.session.get(BookingSecret, booking.id)
    if row is not None and (row.confirmation_code or row.private_notes):
        row.confirmation_code, row.private_notes, row.updated_at = None, None, ctx.now
        await ctx.session.flush()
    _mark_secrets(booking, Secrets(None, None))
    booking.deleted_at = ctx.now
    booking.version += 1
    booking.updated_at = ctx.now
    await ctx.session.flush()
    await _record(ctx, booking, "planning.booking_deleted", operation="delete")


async def reveal(ctx: CommandContext, plan_id: UUID, booking_id: UUID) -> Revealed:
    """The plaintext secrets, for the booking's travelers, creator, and organisers."""

    await planning_access(ctx, plan_id, PlanAction.VIEW)
    booking = await _find(ctx, plan_id, booking_id)
    allowed = (await ctx.session.execute(MAY_REVEAL_SQL, {"booking_id": booking.id})).scalar_one()
    if not allowed:
        raise forbidden("Only the booking's travelers, whoever added it, or an organiser see this")
    row = await ctx.session.get(BookingSecret, booking.id)
    await record_audit(
        ctx,
        action="planning.booking_revealed",
        entity_type=BOOKING_ENTITY,
        entity_id=booking.id,
        plan_id=plan_id,
    )
    if row is None:
        return Revealed(None, None)
    box = secret_box(ctx)
    try:
        return Revealed(
            confirmation_code=_open(box, row, booking.id, CODE, row.confirmation_code),
            private_notes=_open(box, row, booking.id, NOTES, row.private_notes),
        )
    except SecretBoxError as error:
        raise secrets_unreadable() from error


async def booking_views(ctx: CommandContext, bookings: list[Booking]) -> list[BookingView]:
    if not bookings:
        return []
    planned = await costs.live_costs(
        ctx, bookings[0].plan_id, COST_SOURCE, COST_KIND, [booking.id for booking in bookings]
    )
    return [BookingView(booking, planned.get(booking.id)) for booking in bookings]


async def _apply(
    ctx: CommandContext, access: PlanAccess, booking: Booking, draft: BookingDraft
) -> None:
    for day, moment, zone in (
        (draft.start_date, draft.start_time, draft.start_timezone),
        (draft.end_date, draft.end_time, draft.end_timezone),
    ):
        if moment is not None:
            assert day is not None and zone is not None
            resolve_local(datetime.combine(day, moment), zone)
    # An unchanged reference stays valid even after its place was deleted.
    if draft.place_id is not None and draft.place_id != booking.place_id:
        place = await ctx.session.scalar(
            select(Place.id).where(
                Place.plan_id == booking.plan_id,
                Place.id == draft.place_id,
                Place.deleted_at.is_(None),
            )
        )
        if place is None:
            raise validation_error("place_id does not name a saved place of this plan")
    travelers = list(dict.fromkeys(draft.traveler_ids))
    # Newly added travelers are active participants; ones already listed may since have
    # left or been merged and still stay on the booking.
    added = set(travelers) - set(booking.traveler_ids or [])
    if added:
        active = set(
            (
                await ctx.session.execute(
                    select(PlanParticipant.id).where(
                        PlanParticipant.plan_id == booking.plan_id,
                        PlanParticipant.id.in_(added),
                        PlanParticipant.access_state == AccessState.ACTIVE.value,
                    )
                )
            ).scalars()
        )
        if active != added:
            raise validation_error("traveler_ids must name active participants of this plan")
    booking.kind = draft.kind
    booking.title = draft.title
    booking.provider = draft.provider
    booking.start_date, booking.start_time = draft.start_date, draft.start_time
    booking.start_timezone = draft.start_timezone
    booking.end_date, booking.end_time = draft.end_date, draft.end_time
    booking.end_timezone = draft.end_timezone
    booking.place_id = draft.place_id
    booking.traveler_ids = travelers
    booking.status = draft.status
    booking.payment_note = draft.payment_note
    booking.free_cancellation_until = draft.free_cancellation_until


async def _seal(ctx: CommandContext, booking: Booking, secrets: Secrets) -> None:
    """Write the secrets, all under the active key (a kept one is resealed with it)."""

    box = secret_box(ctx)
    row = await ctx.session.get(BookingSecret, booking.id)
    if row is None:
        row = BookingSecret(
            booking_id=booking.id,
            plan_id=booking.plan_id,
            key_id=box.active_key_id,
            confirmation_code=None,
            private_notes=None,
        )
        ctx.session.add(row)
    values: dict[str, str | None] = {}
    for field, wanted, blob in (
        (CODE, secrets.confirmation_code, row.confirmation_code),
        (NOTES, secrets.private_notes, row.private_notes),
    ):
        try:
            values[field] = (
                _open(box, row, booking.id, field, blob) if isinstance(wanted, _Keep) else wanted
            )
        except SecretBoxError as error:
            raise secrets_unreadable() from error
    row.key_id = box.active_key_id
    row.confirmation_code = _sealed(box, booking.id, CODE, values[CODE])
    row.private_notes = _sealed(box, booking.id, NOTES, values[NOTES])
    row.updated_at = ctx.now


def _mark_secrets(booking: Booking, secrets: Secrets | None) -> None:
    """Record on the booking whether each secret is set (``None``/``KEEP`` keep flags)."""

    if secrets is None:
        return
    if not isinstance(secrets.confirmation_code, _Keep):
        booking.has_confirmation_code = secrets.confirmation_code is not None
    if not isinstance(secrets.private_notes, _Keep):
        booking.has_private_notes = secrets.private_notes is not None


def secrets_unreadable() -> BelunoError:
    return conflict(
        "BOOKING_SECRETS_UNREADABLE",
        "This booking's secrets cannot be read",
        "Their key is missing from BELUNO_BOOKING_KEYS or the stored value was altered",
    )


def _sealed(box: SecretBox, owner: UUID, field: str, value: str | None) -> bytes | None:
    return None if value is None else box.seal(value, owner=owner, field=field).blob


def _open(
    box: SecretBox, row: BookingSecret, owner: UUID, field: str, blob: bytes | None
) -> str | None:
    return None if blob is None else box.open(Sealed(row.key_id, blob), owner=owner, field=field)


async def _record_cost(
    ctx: CommandContext, access: PlanAccess, booking: Booking, price: costs.PlannedCost | None
) -> UUID | None:
    source = costs.CostSource(
        source_type=COST_SOURCE,
        source_id=booking.id,
        kind=COST_KIND,
        description=booking.title,
        default_category=KIND_CATEGORY[booking.kind],
    )
    if booking.status == CANCELLED or booking.deleted_at is not None:
        price = None
    tier = CommitmentState.COMMITTED if booking.status == CONFIRMED else CommitmentState.ESTIMATED
    return await costs.sync_cost(ctx, access, source, price, tier=tier)


async def _find(
    ctx: CommandContext, plan_id: UUID, booking_id: UUID, *, for_update: bool = False
) -> Booking:
    statement = select(Booking).where(Booking.plan_id == plan_id, Booking.id == booking_id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    booking = (await ctx.session.execute(statement)).scalar_one_or_none()
    if booking is None or booking.deleted_at is not None:
        raise not_found()
    return booking


async def _record(
    ctx: CommandContext,
    booking: Booking,
    action: str,
    activity: ActivityItem | None = None,
    *,
    operation: str = "upsert",
    secrets_changed: bool = False,
) -> None:
    await record_planning_change(
        ctx,
        action=action,
        entity_type=BOOKING_ENTITY,
        entity_id=booking.id,
        entity_version=booking.version,
        plan_id=booking.plan_id,
        metadata={
            "version": booking.version,
            "status": booking.status,
            "secrets_changed": secrets_changed,
        },
        operation=operation,
        activity=activity,
    )
