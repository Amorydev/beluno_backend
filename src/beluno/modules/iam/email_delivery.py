"""Outbound email port with SMTP and development console adapters.

Only background jobs call these senders; API requests never wait on SMTP.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage
from typing import Protocol

from beluno.config import EmailBackend, Settings, SmtpSecurity


@dataclass(frozen=True)
class OutboundEmail:
    to: str
    subject: str
    text_body: str


class EmailSender(Protocol):
    async def send(self, message: OutboundEmail) -> None: ...


class SmtpEmailSender:
    def __init__(self, settings: Settings) -> None:
        if not settings.smtp_host or not settings.email_from:
            raise RuntimeError("BELUNO_SMTP_HOST and BELUNO_EMAIL_FROM are required for SMTP")
        self._settings = settings

    async def send(self, message: OutboundEmail) -> None:
        await asyncio.to_thread(self._send_blocking, message)

    def _send_blocking(self, message: OutboundEmail) -> None:
        settings = self._settings
        assert settings.smtp_host is not None and settings.email_from is not None
        mime = MimeMessage()
        mime["From"] = settings.email_from
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.text_body)
        context = ssl.create_default_context()
        client: smtplib.SMTP
        if settings.smtp_security is SmtpSecurity.TLS:
            client = smtplib.SMTP_SSL(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout_seconds,
                context=context,
            )
        else:
            client = smtplib.SMTP(
                settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds
            )
        with client:
            if settings.smtp_security is SmtpSecurity.STARTTLS:
                client.starttls(context=context)
            if settings.smtp_username and settings.smtp_password:
                client.login(settings.smtp_username, settings.smtp_password.get_secret_value())
            client.send_message(mime)


class ConsoleEmailSender:
    """Local development only: prints the message so a developer can sign in."""

    async def send(self, message: OutboundEmail) -> None:
        print(f"[beluno email] to={message.to} subject={message.subject}\n{message.text_body}")


class DisabledEmailSender:
    async def send(self, message: OutboundEmail) -> None:
        raise RuntimeError("Email delivery is disabled")


def build_email_sender(settings: Settings) -> EmailSender:
    if settings.email_backend is EmailBackend.SMTP:
        return SmtpEmailSender(settings)
    if settings.email_backend is EmailBackend.CONSOLE:
        return ConsoleEmailSender()
    return DisabledEmailSender()
