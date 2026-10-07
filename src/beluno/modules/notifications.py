"""Push notifications: device tokens, a person's settings, and delivering the outbox.

The database turns activity into notifications (``engagement.fan_out``) and adds
reminders (``engagement.queue_reminders``); the worker then delivers what is due,
outside any transaction, honouring each person's categories and quiet hours in
their own time zone. Security notifications ignore both.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError

from beluno.contracts.errors import version_conflict
from beluno.db.models.engagement import Notification, NotificationSettings, PushToken
from beluno.modules.context import CommandContext, Runtime
from beluno.modules.sync_audit.recorder import record_audit
from beluno.push import PushMessage, PushResult, PushSender

FAN_OUT = text("SELECT engagement.fan_out(:now)")
QUEUE_REMINDERS = text("SELECT engagement.queue_reminders(:now)")
REGISTER_TOKEN = text("SELECT engagement.register_push_token(:session_id, :token, :platform)")
FORGET_SETTINGS = text("SELECT engagement.forget_settings()")
LIVE_TOKENS = text(
    "SELECT id, user_id, token, platform FROM engagement.live_tokens(CAST(:users AS uuid[]), :now)"
)
BATCH = 100
MAX_ATTEMPTS = 5
STUCK_SENDING = timedelta(minutes=10)
KEEP_FOR = timedelta(days=30)
CATEGORIES = ("money", "reminders", "summaries", "news")


@dataclass(frozen=True)
class Preferences:
    money: bool = True
    reminders: bool = True
    summaries: bool = True
    news: bool = False
    quiet_hours: bool = True
    quiet_start: time = time(22, 0)
    quiet_end: time = time(7, 0)
    version: int = 0  # 0: never saved, the defaults apply

    def allows(self, category: str) -> bool:
        return category == "security" or bool(getattr(self, category, False))


DEFAULTS = Preferences()


# --- API: tokens and settings ------------------------------------------------------------


async def register_token(ctx: CommandContext, token: str, platform: str) -> None:
    """Bind a push token to the caller's current session (moving it from any other)."""

    actor = ctx.require_actor()
    await ctx.session.execute(
        REGISTER_TOKEN,
        {"session_id": actor.session_id, "token": token, "platform": platform},
    )


async def forget_token(ctx: CommandContext) -> None:
    actor = ctx.require_actor()
    await ctx.session.execute(
        delete(PushToken).where(
            PushToken.user_id == actor.user_id, PushToken.session_id == actor.session_id
        )
    )


async def get_preferences(ctx: CommandContext) -> Preferences:
    row = await ctx.session.get(NotificationSettings, ctx.require_actor().user_id)
    return _preferences(row) if row is not None else DEFAULTS


async def save_preferences(
    ctx: CommandContext, expected_version: int, wanted: Preferences
) -> Preferences:
    """Replace the caller's settings; ``expected_version`` 0 means never saved before."""

    user_id = ctx.require_actor().user_id
    row = (
        await ctx.session.execute(
            select(NotificationSettings)
            .where(NotificationSettings.user_id == user_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    current = row.version if row is not None else 0
    if current != expected_version:
        raise version_conflict(row)
    values = {
        "money": wanted.money,
        "reminders": wanted.reminders,
        "summaries": wanted.summaries,
        "news": wanted.news,
        "quiet_hours": wanted.quiet_hours,
        "quiet_start": wanted.quiet_start,
        "quiet_end": wanted.quiet_end,
        "version": current + 1,
        "updated_at": ctx.now,
    }
    if row is None:
        row = NotificationSettings(user_id=user_id, **values)
        try:
            async with ctx.savepoint():
                ctx.session.add(row)
                await ctx.session.flush()
        except IntegrityError as error:
            # Saved at the same moment from another device: that one counts.
            raise version_conflict(None) from error
    else:
        for name, value in values.items():
            setattr(row, name, value)
        await ctx.session.flush()
    await record_audit(
        ctx, action="notifications.settings_saved", entity_type="user", entity_id=user_id
    )
    return _preferences(row)


async def forget_settings(ctx: CommandContext) -> None:
    """Account deletion: settings and undelivered notifications go."""

    await ctx.session.execute(FORGET_SETTINGS)


def quiet_until(now: datetime, timezone: str | None, preferences: Preferences) -> datetime | None:
    """When quiet hours end, if ``now`` falls inside them in the person's zone."""

    if not preferences.quiet_hours or preferences.quiet_start == preferences.quiet_end:
        return None
    try:
        zone = ZoneInfo(timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    local = now.astimezone(zone)
    start, end, clock = preferences.quiet_start, preferences.quiet_end, local.time()
    wraps = start > end  # e.g. 22:00-07:00
    inside = (clock >= start or clock < end) if wraps else (start <= clock < end)
    if not inside:
        return None
    day = local.date() if clock < end else local.date() + timedelta(days=1)
    return datetime.combine(day, end, tzinfo=zone)


# --- worker: fan out, remind, deliver ------------------------------------------------------


async def dispatch(runtime: Runtime, sender: PushSender) -> int:
    """Every minute: turn activity into notifications, add reminders, deliver what is due."""

    now = runtime.clock()
    async with runtime.database.transaction() as session:
        await session.execute(FAN_OUT, {"now": now})
        await session.execute(QUEUE_REMINDERS, {"now": now})
        # Deliveries a crashed worker left half-done go back to the queue, or fail once
        # they have used up their attempts.
        stuck = (Notification.state == "sending") & (
            Notification.deliver_after < now - STUCK_SENDING
        )
        await session.execute(
            update(Notification)
            .where(stuck, Notification.attempts < MAX_ATTEMPTS)
            .values(state="pending")
        )
        await session.execute(
            update(Notification)
            .where(stuck, Notification.attempts >= MAX_ATTEMPTS)
            .values(state="failed")
        )
        await session.execute(
            delete(Notification).where(
                Notification.state.in_(("sent", "skipped", "failed")),
                Notification.created_at < now - KEEP_FOR,
            )
        )
    return await deliver(runtime, sender, now)


@dataclass(frozen=True)
class _Send:
    notification_id: UUID
    attempts: int
    token_id: UUID
    message: PushMessage


async def deliver(runtime: Runtime, sender: PushSender, now: datetime) -> int:
    outgoing: list[_Send] = []
    async with runtime.database.transaction() as session:
        due = list(
            (
                await session.execute(
                    select(Notification)
                    .where(Notification.state == "pending", Notification.deliver_after <= now)
                    .order_by(Notification.deliver_after, Notification.id)
                    .limit(BATCH)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        people = {row.user_id for row in due}
        settings = {
            row.user_id: _preferences(row)
            for row in (
                await session.execute(
                    select(NotificationSettings).where(NotificationSettings.user_id.in_(people))
                )
            ).scalars()
        }
        tokens: dict[UUID, list[Device]] = {}
        for row in await session.execute(LIVE_TOKENS, {"users": list(people), "now": now}):
            tokens.setdefault(row.user_id, []).append(Device(row.id, row.token, row.platform))
        for notification in due:
            if notification.expires_at is not None and notification.expires_at <= now:
                notification.state = "skipped"  # a reminder that came too late
                continue
            preferences = settings.get(notification.user_id, DEFAULTS)
            if not preferences.allows(notification.category):
                notification.state = "skipped"
                continue
            later = (
                quiet_until(now, notification.timezone, preferences)
                if notification.category != "security"
                else None
            )
            if later is not None:
                notification.deliver_after = later
                continue
            devices = tokens.get(notification.user_id, [])
            if not devices:
                notification.state = "skipped"
                continue
            notification.state = "sending"
            notification.attempts += 1
            notification.deliver_after = now
            outgoing.extend(
                _Send(
                    notification.id,
                    notification.attempts,
                    device.id,
                    _message(notification, device),
                )
                for device in devices
            )
        await session.flush()

    results: dict[UUID, list[PushResult]] = {}
    invalid: list[UUID] = []
    attempts: dict[UUID, int] = {}
    answers = await sender.send_many([send.message for send in outgoing]) if outgoing else []
    for send, result in zip(outgoing, answers, strict=True):
        results.setdefault(send.notification_id, []).append(result)
        attempts[send.notification_id] = send.attempts
        if result is PushResult.INVALID_TOKEN:
            invalid.append(send.token_id)

    async with runtime.database.transaction() as session:
        for notification_id, outcomes in results.items():
            tried = attempts[notification_id]
            if PushResult.DELIVERED in outcomes:
                state, after = "sent", now
            elif PushResult.RETRY in outcomes and tried < MAX_ATTEMPTS:
                state, after = "pending", now + timedelta(minutes=2**tried)
            elif PushResult.NOT_CONFIGURED in outcomes:
                state, after = "skipped", now
            else:
                state, after = "failed", now
            await session.execute(
                update(Notification)
                .where(Notification.id == notification_id)
                .values(
                    state=state,
                    deliver_after=after,
                    sent_at=now if state == "sent" else None,
                )
            )
        if invalid:
            await session.execute(delete(PushToken).where(PushToken.id.in_(invalid)))
    return sum(1 for outcomes in results.values() if PushResult.DELIVERED in outcomes)


@dataclass(frozen=True)
class Device:
    id: UUID
    token: str
    platform: str


def _message(notification: Notification, device: Device) -> PushMessage:
    args = notification.args
    # Always the same arguments per category, so the app's strings line up.
    keys = ("actor", "plan") if notification.category == "money" else ("plan",)
    loc_args = [str(args.get(key) or "") for key in keys]
    data = {"notification_id": str(notification.id)}
    for key, value in (
        ("plan_id", notification.plan_id),
        ("entity_type", notification.entity_type),
        ("entity_id", notification.entity_id),
    ):
        if value is not None:
            data[key] = str(value)
    return PushMessage(
        token=device.token,
        platform=device.platform,
        kind=notification.kind,
        category=notification.category,
        loc_args=loc_args,
        data=data,
        thread_id=str(notification.plan_id) if notification.plan_id else None,
    )


def _preferences(row: NotificationSettings) -> Preferences:
    return Preferences(
        money=row.money,
        reminders=row.reminders,
        summaries=row.summaries,
        news=row.news,
        quiet_hours=row.quiet_hours,
        quiet_start=row.quiet_start,
        quiet_end=row.quiet_end,
        version=row.version,
    )
