from __future__ import annotations

import json
from datetime import UTC, datetime, time
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from firebase_admin import exceptions, messaging

from beluno.modules.notifications import DEFAULTS, Preferences, quiet_until
from beluno.push import FirebasePushSender, PushMessage, PushResult, build_message


def test_quiet_hours_wrap_midnight_in_the_persons_zone() -> None:
    # 23:30 in Ho Chi Minh City (16:30 UTC): quiet until 07:00 local the next day.
    late = datetime(2027, 3, 20, 16, 30, tzinfo=UTC)
    until = quiet_until(late, "Asia/Ho_Chi_Minh", DEFAULTS)
    assert until is not None
    assert until.astimezone(UTC) == datetime(2027, 3, 21, 0, 0, tzinfo=UTC)
    # 06:00 local: quiet until 07:00 the same day.
    early = datetime(2027, 3, 20, 23, 0, tzinfo=UTC)
    assert quiet_until(early, "Asia/Ho_Chi_Minh", DEFAULTS) == datetime(
        2027, 3, 21, 0, 0, tzinfo=UTC
    )
    # Noon local is not quiet; nor is anything when quiet hours are off.
    noon = datetime(2027, 3, 20, 5, 0, tzinfo=UTC)
    assert quiet_until(noon, "Asia/Ho_Chi_Minh", DEFAULTS) is None
    assert quiet_until(late, "Asia/Ho_Chi_Minh", Preferences(quiet_hours=False)) is None
    # A daytime window, and an unknown zone counted as UTC.
    lunch = Preferences(quiet_start=time(12, 0), quiet_end=time(13, 0))
    assert quiet_until(datetime(2027, 3, 20, 12, 30, tzinfo=UTC), "Mars/Base", lunch) == datetime(
        2027, 3, 20, 13, 0, tzinfo=UTC
    )


def test_categories_and_security() -> None:
    assert DEFAULTS.allows("money") and not DEFAULTS.allows("news")
    assert Preferences(money=False).allows("security")


def test_messages_carry_keys_not_text() -> None:
    built = build_message(
        PushMessage(
            token="t" * 30,
            platform="ios",
            kind="expense_added",
            category="money",
            loc_args=["Ann", "Japan 2027"],
            data={"plan_id": "p"},
            thread_id="p",
        )
    )
    alert = built.apns.payload.aps.alert
    assert (alert.loc_key, alert.loc_args) == (
        "notification_expense_added_body",
        ["Ann", "Japan 2027"],
    )
    assert alert.body is None and alert.title is None
    android = built.android.notification
    assert (android.body_loc_key, android.channel_id, android.visibility) == (
        "notification_expense_added_body",
        "money",
        "private",
    )
    assert built.data == {"plan_id": "p", "kind": "expense_added", "category": "money"}


def service_account() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return json.dumps(
        {
            "type": "service_account",
            "project_id": "beluno-test",
            "private_key_id": "k1",
            "private_key": pem,
            "client_email": "push@beluno-test.iam.gserviceaccount.com",
            "client_id": "1",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (None, PushResult.DELIVERED),
        (messaging.UnregisteredError("gone"), PushResult.INVALID_TOKEN),
        (messaging.SenderIdMismatchError("other app"), PushResult.INVALID_TOKEN),
        (exceptions.InvalidArgumentError("bad"), PushResult.REJECTED),
        (messaging.QuotaExceededError("slow down"), PushResult.RETRY),
        (messaging.ThirdPartyAuthError("APNs key"), PushResult.RETRY),
        (exceptions.UnavailableError("down"), PushResult.RETRY),
    ],
)
async def test_fcm_answers_map_to_results(
    monkeypatch: pytest.MonkeyPatch, raised: Exception | None, expected: PushResult
) -> None:
    def send_each(messages: list[messaging.Message], app: object = None) -> object:
        assert [m.token for m in messages] == ["t" * 30, "u" * 30]
        first = SimpleNamespace(success=raised is None, exception=raised)
        return SimpleNamespace(responses=[first, SimpleNamespace(success=True, exception=None)])

    monkeypatch.setattr(messaging, "send_each", send_each)
    sender = FirebasePushSender(service_account())
    messages = [
        PushMessage("t" * 30, "android", "task_due", "reminders", ["Trip"]),
        PushMessage("u" * 30, "ios", "task_due", "reminders", ["Trip"]),
    ]
    assert await sender.send_many(messages) == [expected, PushResult.DELIVERED]


async def test_an_unreachable_fcm_retries_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    def send_each(messages: list[messaging.Message], app: object = None) -> object:
        raise TimeoutError("no answer")

    monkeypatch.setattr(messaging, "send_each", send_each)
    sender = FirebasePushSender(service_account())
    message = PushMessage("t" * 30, "android", "task_due", "reminders", ["Trip"])
    assert await sender.send_many([message, message]) == [PushResult.RETRY] * 2
