from __future__ import annotations

import secrets

import pytest
from pydantic import ValidationError

from beluno.config import EmailBackend, Environment, ProcessRole, Settings, SmtpSecurity
from beluno.secret_box import new_keyring_json
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
        "booking_keys": new_keyring_json("k1"),
        "email_backend": EmailBackend.SMTP,
        "smtp_host": "smtp.example.com",
        "email_from": "Beluno <no-reply@example.com>",
        "storage_endpoint_url": "http://rustfs:9000",
        "storage_public_url": "https://media.beluno.example.com",
        "storage_access_key_id": "beluno",
        "storage_secret_access_key": secrets.token_urlsafe(30),
        "clamd_host": "clamav",
        "webauthn_rp_id": "beluno.example.com",
        "webauthn_origins": ["https://beluno.example.com", "android:apk-key-hash:abc"],
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


def test_secure_environments_need_the_passkey_relying_party() -> None:
    with pytest.raises(RuntimeError, match="WEBAUTHN_RP_ID"):
        secure_settings(webauthn_rp_id="localhost").assert_runtime_requirements()
    for rp_id in ("LOCALHOST", "127.0.0.1", "https://beluno.example.com", "beluno"):
        with pytest.raises(RuntimeError, match="WEBAUTHN_RP_ID"):
            secure_settings(webauthn_rp_id=rp_id).assert_runtime_requirements()
    for origins in (
        ["http://beluno.example.com"],
        ["https://evil.example.org"],
        ["https://notbeluno.example.com.evil.io"],
        [],
    ):
        with pytest.raises(RuntimeError, match="WEBAUTHN_ORIGINS"):
            secure_settings(webauthn_origins=origins).assert_runtime_requirements()
    secure_settings(
        webauthn_origins=["https://app.beluno.example.com", "android:apk-key-hash:abc"]
    ).assert_runtime_requirements()
    # Workers issue no passkey challenges.
    secure_settings(
        process_role=ProcessRole.WORKER, webauthn_rp_id="localhost"
    ).assert_runtime_requirements()


def test_secure_environments_need_storage_and_a_scanner() -> None:
    with pytest.raises(RuntimeError, match="BELUNO_STORAGE_SECRET_ACCESS_KEY"):
        secure_settings(storage_secret_access_key=None).assert_runtime_requirements()
    with pytest.raises(RuntimeError, match="BELUNO_CLAMD_HOST"):
        secure_settings(clamd_host=None).assert_runtime_requirements()
    # The API never scans, so it does not need the scanner.
    secure_settings(process_role=ProcessRole.API, clamd_host=None).assert_runtime_requirements()
    with pytest.raises(RuntimeError, match="https"):
        secure_settings(storage_public_url="http://media.example.com").assert_runtime_requirements()


def test_auth_requires_signing_keys_and_hash_key() -> None:
    settings = Settings(_env_file=None, auth_signing_keys="[]")  # type: ignore[call-arg]
    assert settings.auth_is_configured is False


def test_short_token_hash_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(token_hash_key="too-short")


def test_sync_retention_and_page_size_are_bounded() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, sync_pull_page_size=1_000)  # type: ignore[call-arg]
    with pytest.raises(RuntimeError, match="OFFLINE_WINDOW"):
        Settings(  # type: ignore[call-arg]
            _env_file=None, sync_offline_window_days=90, sync_change_retention_days=30
        ).assert_retention_requirements()
    Settings(_env_file=None, sync_pull_page_size=500).assert_retention_requirements()  # type: ignore[call-arg]


def test_each_process_needs_only_its_own_database_url() -> None:
    names = {
        ProcessRole.API: "api_database_url",
        ProcessRole.WORKER: "worker_database_url",
        ProcessRole.SCHEDULER: "scheduler_database_url",
        ProcessRole.MIGRATE: "migration_database_url",
    }
    others = dict.fromkeys(names.values())
    for role, own in names.items():
        alone = {**others, own: "postgresql+psycopg://a:b@db/beluno?sslmode=require"}
        secure_settings(process_role=role, **alone).assert_runtime_requirements()
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            secure_settings(process_role=role, **others).assert_runtime_requirements()
    with pytest.raises(RuntimeError, match="BELUNO_MIGRATION_DATABASE_URL"):
        secure_settings(migration_database_url=None).assert_runtime_requirements()


def test_migrations_and_the_scheduler_need_no_keys_or_email() -> None:
    bare = {
        "api_database_url": None,
        "worker_database_url": None,
        "scheduler_database_url": None,
        "migration_database_url": None,
        "auth_signing_keys": None,
        "token_hash_key": None,
        "email_backend": EmailBackend.CONSOLE,
    }
    url = "postgresql+psycopg://a:b@db/beluno?sslmode=require"
    secure_settings(
        process_role=ProcessRole.MIGRATE, **{**bare, "migration_database_url": url}
    ).assert_runtime_requirements()
    secure_settings(
        process_role=ProcessRole.SCHEDULER, **{**bare, "scheduler_database_url": url}
    ).assert_runtime_requirements()
    with pytest.raises(RuntimeError, match="BELUNO_AUTH_SIGNING_KEYS"):
        secure_settings(
            process_role=ProcessRole.WORKER, **{**bare, "worker_database_url": url}
        ).assert_runtime_requirements()


def test_the_api_needs_a_booking_keyring_and_a_broken_one_is_refused() -> None:
    with pytest.raises(RuntimeError, match="BELUNO_BOOKING_KEYS"):
        secure_settings(booking_keys=None).assert_runtime_requirements()
    secure_settings(
        booking_keys=None, process_role=ProcessRole.WORKER
    ).assert_runtime_requirements()
    with pytest.raises(ValueError, match="keyring"):
        secure_settings(booking_keys='{"active": "k1", "keys": {"k1": "short"}}')


def test_a_configured_app_store_verifies_fully_in_secure_environments() -> None:
    store = {"apple_bundle_id": "app.beluno", "apple_root_certificates": ["/etc/apple.cer"]}
    secure_settings(**store).assert_runtime_requirements()
    with pytest.raises(RuntimeError, match="ONLINE_CHECKS"):
        secure_settings(**store, apple_online_checks=False).assert_runtime_requirements()
    with pytest.raises(RuntimeError, match="ROOT_CERTIFICATES"):
        secure_settings(apple_bundle_id="app.beluno").assert_runtime_requirements()
    with pytest.raises(RuntimeError, match="APP_APPLE_ID"):
        secure_settings(**store, apple_environment="Production").assert_runtime_requirements()
    secure_settings(
        **store, apple_environment="Production", apple_app_apple_id=1
    ).assert_runtime_requirements()
