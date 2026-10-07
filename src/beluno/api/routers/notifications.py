"""This device's push token and the caller's notification settings."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status

from beluno.api.dependencies import ActorDep, RuntimeDep
from beluno.api.http import IfMatch, parse_if_match, set_etag
from beluno.api.problems import problem_responses
from beluno.contracts.notifications import (
    NotificationSettingsBody,
    NotificationSettingsResponse,
    NudgeResponse,
    PaymentNudgeRequest,
    PushTokenRequest,
)
from beluno.modules import notifications
from beluno.modules.context import open_context
from beluno.modules.iam import rate_limits

router = APIRouter(tags=["notifications"])

ERRORS = problem_responses(401, 409, 412, 422, 428, 503)


@router.put("/v1/me/push-token", status_code=status.HTTP_204_NO_CONTENT, responses=ERRORS)
async def register_push_token(
    body: PushTokenRequest, runtime: RuntimeDep, actor: ActorDep
) -> Response:
    """Send notifications for this account to this device (until sign-out)."""

    async with open_context(runtime, actor) as ctx:
        await notifications.register_token(ctx, body.token, body.platform)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/v1/me/push-token", status_code=status.HTTP_204_NO_CONTENT, responses=ERRORS)
async def forget_push_token(runtime: RuntimeDep, actor: ActorDep) -> Response:
    async with open_context(runtime, actor) as ctx:
        await notifications.forget_token(ctx)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/v1/me/notification-settings", response_model=NotificationSettingsResponse, responses=ERRORS
)
async def get_notification_settings(
    runtime: RuntimeDep, actor: ActorDep, response: Response
) -> NotificationSettingsResponse:
    async with open_context(runtime, actor) as ctx:
        found = await notifications.get_preferences(ctx)
    set_etag(response, found.version)
    return _response(found)


@router.put(
    "/v1/me/notification-settings", response_model=NotificationSettingsResponse, responses=ERRORS
)
async def save_notification_settings(
    body: NotificationSettingsBody,
    runtime: RuntimeDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
) -> NotificationSettingsResponse:
    """Replace the settings; send the version read (``"0"`` before the first save)."""

    expected = parse_if_match(if_match)
    async with open_context(runtime, actor) as ctx:
        saved = await notifications.save_preferences(
            ctx, expected, notifications.Preferences(**body.model_dump())
        )
    set_etag(response, saved.version)
    return _response(saved)


def _response(found: notifications.Preferences) -> NotificationSettingsResponse:
    return NotificationSettingsResponse(
        money=found.money,
        reminders=found.reminders,
        summaries=found.summaries,
        news=found.news,
        quiet_hours=found.quiet_hours,
        quiet_start=found.quiet_start,
        quiet_end=found.quiet_end,
        version=found.version,
    )


NUDGE_ERRORS = problem_responses(401, 403, 404, 409, 422, 429, 503)


@router.post(
    "/v1/plans/{plan_id}/tasks/{task_id}/nudge",
    response_model=NudgeResponse,
    responses=NUDGE_ERRORS,
)
async def nudge_task(
    plan_id: UUID, task_id: UUID, runtime: RuntimeDep, actor: ActorDep
) -> NudgeResponse:
    """Remind a task's assignee (whoever added the task, or an organiser; once a day)."""

    await rate_limits.enforce_rate_limit(runtime, rate_limits.NUDGES_PER_USER, str(actor.user_id))
    async with open_context(runtime, actor) as ctx:
        queued = await notifications.nudge_task(ctx, plan_id, task_id)
    return NudgeResponse(queued=queued)


@router.post(
    "/v1/plans/{plan_id}/ledger/nudges", response_model=NudgeResponse, responses=NUDGE_ERRORS
)
async def nudge_payment(
    plan_id: UUID, body: PaymentNudgeRequest, runtime: RuntimeDep, actor: ActorDep
) -> NudgeResponse:
    """Remind someone who owes you in this plan (once a day per person)."""

    await rate_limits.enforce_rate_limit(runtime, rate_limits.NUDGES_PER_USER, str(actor.user_id))
    async with open_context(runtime, actor) as ctx:
        queued = await notifications.nudge_payment(ctx, plan_id, body.participant_id)
    return NudgeResponse(queued=queued)
