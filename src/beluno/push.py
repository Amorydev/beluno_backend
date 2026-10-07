"""Push delivery through Firebase Cloud Messaging (Android and iOS, via APNs).

Messages carry localisation keys and a few safe arguments (who, which plan): the app
renders the text in the phone's language, and the server never puts amounts, codes,
or addresses on a lock screen. Keys are ``notification_<kind>_title`` and
``notification_<kind>_body``. ``firebase-admin`` is synchronous: sends run in a
thread.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

import anyio
import firebase_admin
from firebase_admin import credentials, exceptions, messaging

from beluno.config import Settings

HTTP_TIMEOUT_SECONDS = 10


class PushResult(StrEnum):
    DELIVERED = "delivered"
    INVALID_TOKEN = "invalid_token"  # the app was uninstalled or the token rotated
    RETRY = "retry"  # FCM is busy or unavailable
    REJECTED = "rejected"  # the message itself was refused; retrying will not help
    NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True)
class PushMessage:
    token: str
    platform: str
    kind: str
    category: str
    loc_args: list[str]
    data: dict[str, str] = field(default_factory=dict)
    thread_id: str | None = None  # groups a plan's notifications on iOS
    # News from the team carries its own words; every other kind a localisation key.
    title: str | None = None
    body: str | None = None


class PushSender(Protocol):
    async def send_many(self, messages: list[PushMessage]) -> list[PushResult]:
        """One result per message, in order."""
        ...


def build_message(message: PushMessage) -> messaging.Message:
    title_key = f"notification_{message.kind}_title"
    body_key = f"notification_{message.kind}_body"
    written = message.title is not None
    keys: dict[str, object] = (
        {}
        if written
        else {
            "title_loc_key": title_key,
            "title_loc_args": message.loc_args,
            "body_loc_key": body_key,
            "body_loc_args": message.loc_args,
        }
    )
    alert = (
        messaging.ApsAlert(title=message.title, body=message.body)
        if written
        else messaging.ApsAlert(
            title_loc_key=title_key,
            title_loc_args=message.loc_args,
            loc_key=body_key,
            loc_args=message.loc_args,
        )
    )
    return messaging.Message(
        token=message.token,
        data={**message.data, "kind": message.kind, "category": message.category},
        android=messaging.AndroidConfig(
            priority="high" if message.category in ("money", "security") else "normal",
            notification=messaging.AndroidNotification(
                title=message.title,
                body=message.body,
                channel_id=message.category,
                # The lock screen shows only that something happened.
                visibility="private",
                **keys,
            ),
        ),
        apns=messaging.APNSConfig(
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    alert=alert,
                    thread_id=message.thread_id,
                    category=message.kind,
                    sound="default",
                )
            )
        ),
    )


class FirebasePushSender:
    def __init__(self, service_account_json: str) -> None:
        certificate = credentials.Certificate(json.loads(service_account_json))
        # A name of its own: a second sender (tests, reloads) never clashes. A short
        # HTTP timeout keeps one slow FCM call from holding the worker.
        self._app = firebase_admin.initialize_app(
            certificate, {"httpTimeout": HTTP_TIMEOUT_SECONDS}, name=f"beluno-push-{uuid4()}"
        )

    async def send_many(self, messages: list[PushMessage]) -> list[PushResult]:
        built = [build_message(message) for message in messages]
        try:
            batch = await anyio.to_thread.run_sync(
                lambda: messaging.send_each(built, app=self._app)
            )
        except Exception:
            return [PushResult.RETRY] * len(messages)
        return [
            PushResult.DELIVERED if answer.success else result_of(answer.exception)
            for answer in batch.responses
        ]


def result_of(error: BaseException | None) -> PushResult:
    """What one FCM refusal means for that message and its token."""

    if isinstance(error, (messaging.UnregisteredError, messaging.SenderIdMismatchError)):
        return PushResult.INVALID_TOKEN
    if isinstance(error, exceptions.InvalidArgumentError):
        return PushResult.REJECTED
    # Quotas, outages, credentials (an APNs key, the service account): retry, capped.
    return PushResult.RETRY


class DisabledPushSender:
    """No FCM credentials: notifications are recorded, not sent."""

    async def send_many(self, messages: list[PushMessage]) -> list[PushResult]:
        return [PushResult.NOT_CONFIGURED] * len(messages)


def build_push_sender(settings: Settings) -> PushSender:
    if settings.fcm_service_account_json is None:
        return DisabledPushSender()
    return FirebasePushSender(settings.fcm_service_account_json.get_secret_value())
