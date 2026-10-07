"""Settings and helpers for suites that run against a live PostgreSQL database."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from pydantic import SecretStr

from beluno.config import EmailBackend, Environment, Settings
from beluno.modules.iam.email_delivery import OutboundEmail
from beluno.secret_box import new_keyring_json
from beluno.testkit import media as media_testkit
from beluno.testkit.identity import APPLE_CLIENT_ID, GOOGLE_CLIENT_ID
from beluno.testkit.media import MediaServices

# Synthetic credentials for disposable local/CI databases only.
RUNTIME_PASSWORDS = {
    "migrator": "migrator_password",
    "api_runtime": "api_password",
    "worker_runtime": "worker_password",
    "scheduler_runtime": "scheduler_password",
}


def role_dsn(admin_dsn: str, role: str) -> str:
    parsed = urlparse(admin_dsn)
    netloc = f"{role}:{RUNTIME_PASSWORDS[role]}@{parsed.hostname or 'localhost'}"
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(("postgresql+psycopg", netloc, parsed.path, "", "", ""))


def generate_signing_keys_json(kid: str = "test-key") -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode("ascii")
    return json.dumps([{"kid": kid, "private_key_pem": pem}])


def build_settings(
    *,
    api_dsn: str,
    worker_dsn: str,
    scheduler_dsn: str,
    migration_dsn: str,
    media: MediaServices | None = None,
) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment=Environment.TEST,
        api_database_url=SecretStr(api_dsn),
        worker_database_url=SecretStr(worker_dsn),
        scheduler_database_url=SecretStr(scheduler_dsn),
        migration_database_url=SecretStr(migration_dsn),
        auth_signing_keys=SecretStr(generate_signing_keys_json()),
        token_hash_key=SecretStr(secrets.token_urlsafe(48)),
        booking_keys=SecretStr(new_keyring_json("test")),
        auth_google_client_ids=[GOOGLE_CLIENT_ID],
        auth_apple_client_ids=[APPLE_CLIENT_ID],
        auth_magic_link_url="https://app.beluno.test/auth/email",
        email_backend=EmailBackend.CONSOLE,
        storage_endpoint_url=media.storage_url if media else None,
        storage_bucket=media_testkit.BUCKET,
        storage_region=media_testkit.REGION,
        storage_access_key_id=SecretStr(media_testkit.ACCESS_KEY_ID) if media else None,
        storage_secret_access_key=SecretStr(media_testkit.SECRET_ACCESS_KEY) if media else None,
        clamd_host=media.clamd_host if media else None,
        clamd_port=media.clamd_port if media else 3310,
    )


@dataclass
class IntegrationEnvironment:
    settings: Settings
    admin_dsn: str


@dataclass
class RecordingEmailSender:
    """Captures outbound email so tests can read the one-time code."""

    messages: list[OutboundEmail] = field(default_factory=list)

    async def send(self, message: OutboundEmail) -> None:
        self.messages.append(message)
