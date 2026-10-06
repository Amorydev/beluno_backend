from __future__ import annotations

import secrets

import pytest
from pydantic import ValidationError

from beluno.config import EmailBackend, Environment, Settings, SmtpSecurity
from beluno.testkit.environment import generate_signing_keys_json


def secure_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": Environment.PRODUCTION,
        "api_database_url": "postgresql+psycopg://a:b@db/beluno?sslmode=require",
        "worker_database_url": "postgresql+psycopg://a:b@db/beluno?sslmode=require",
        "scheduler_database_url": "postgresql+psycopg://a:b@db/beluno?sslmode=require",
        "migration_database_url": "postgresql+psycopg://a:b@db/beluno?sslmode=require",
        "auth_signing_keys": generate_signing_keys_json(),
        "token_hash_key": secrets.token_urlsafe(40),
        "email_backend": EmailBackend.SMTP,
        "smtp_host": "smtp.example.com",
        "email_from": "Beluno <no-reply@example.com>",
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg, arg-type]


def test_rejects_non_postgres_database_url() -> None:
    with pytest.raises(ValidationError):
        Settings(api_database_url="sqlite:///beluno.db")


def test_production_requires_database_and_auth_configuration() -> None:
    settings = Settings(_env_file=None, environment=Environment.PRODUCTION)  # type: ignore[call-arg]
    with pytest.raises(RuntimeError, match="BELUNO_API_DATABASE_URL") as captured:
        settings.assert_production_requirements()
    assert "BELUNO_AUTH_SIGNING_KEYS" in str(captured.value)
    assert "BELUNO_TOKEN_HASH_KEY" in str(captured.value)


def test_complete_secure_configuration_passes() -> None:
    secure_settings().assert_runtime_requirements()


def test_secure_environments_reject_console_email_and_plaintext_smtp() -> None:
    with pytest.raises(RuntimeError, match="console"):
        secure_settings(email_backend=EmailBackend.CONSOLE).assert_runtime_requirements()
    with pytest.raises(RuntimeError, match="SMTP_SECURITY"):
        secure_settings(smtp_security=SmtpSecurity.NONE).assert_runtime_requirements()
    with pytest.raises(RuntimeError, match="https"):
        secure_settings(auth_magic_link_url="http://app.example.com").assert_runtime_requirements()


def test_auth_requires_signing_keys_and_hash_key() -> None:
    settings = Settings(_env_file=None, auth_signing_keys="[]")  # type: ignore[call-arg]
    assert settings.auth_is_configured is False


def test_short_token_hash_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(token_hash_key="too-short")
