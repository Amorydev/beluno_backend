from __future__ import annotations

import smtplib
from typing import Any, ClassVar

import pytest

from beluno.config import EmailBackend, Settings, SmtpSecurity
from beluno.modules.iam.email_delivery import (
    ConsoleEmailSender,
    DisabledEmailSender,
    OutboundEmail,
    SmtpEmailSender,
    build_email_sender,
)

MESSAGE = OutboundEmail(to="ana@example.com", subject="Your code", text_body="123456")


class RecordingSmtp:
    instances: ClassVar[list[RecordingSmtp]] = []

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        self.host, self.port, self.kwargs = host, port, kwargs
        self.calls: list[str] = []
        self.sent: list[Any] = []
        RecordingSmtp.instances.append(self)

    def __enter__(self) -> RecordingSmtp:
        return self

    def __exit__(self, *_: object) -> None:
        self.calls.append("quit")

    def starttls(self, **_: object) -> None:
        self.calls.append("starttls")

    def login(self, username: str, password: str) -> None:
        self.calls.append(f"login:{username}:{bool(password)}")

    def send_message(self, message: Any) -> None:
        self.sent.append(message)


def smtp_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "email_backend": EmailBackend.SMTP,
        "smtp_host": "smtp.example.com",
        "email_from": "Beluno <no-reply@example.com>",
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg, arg-type]


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingSmtp.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", RecordingSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", RecordingSmtp)


async def test_starttls_delivery_authenticates_and_sends() -> None:
    sender = build_email_sender(smtp_settings(smtp_username="mailer", smtp_password="secret"))
    assert isinstance(sender, SmtpEmailSender)

    await sender.send(MESSAGE)

    client = RecordingSmtp.instances[0]
    assert (client.host, client.port) == ("smtp.example.com", 587)
    assert client.calls == ["starttls", "login:mailer:True", "quit"]
    sent = client.sent[0]
    assert (sent["To"], sent["Subject"]) == ("ana@example.com", "Your code")
    assert sent.get_content().strip() == "123456"


async def test_implicit_tls_skips_starttls_and_anonymous_login() -> None:
    await SmtpEmailSender(smtp_settings(smtp_security=SmtpSecurity.TLS, smtp_port=465)).send(
        MESSAGE
    )
    client = RecordingSmtp.instances[0]
    assert "context" in client.kwargs
    assert client.calls == ["quit"]


def test_smtp_requires_host_and_sender() -> None:
    with pytest.raises(RuntimeError, match="SMTP_HOST"):
        SmtpEmailSender(Settings(_env_file=None))  # type: ignore[call-arg]


async def test_console_and_disabled_backends(capsys: pytest.CaptureFixture[str]) -> None:
    console = build_email_sender(Settings(_env_file=None))  # type: ignore[call-arg]
    assert isinstance(console, ConsoleEmailSender)
    await console.send(MESSAGE)
    assert "123456" in capsys.readouterr().out

    disabled = build_email_sender(
        Settings(_env_file=None, email_backend=EmailBackend.DISABLED)  # type: ignore[call-arg]
    )
    assert isinstance(disabled, DisabledEmailSender)
    with pytest.raises(RuntimeError):
        await disabled.send(MESSAGE)
