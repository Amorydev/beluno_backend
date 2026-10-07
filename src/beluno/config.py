"""Typed configuration and environment safety checks."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from urllib.parse import parse_qs, urlparse, urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class EmailBackend(StrEnum):
    """``console`` prints messages for local development only; ``smtp`` delivers them."""

    CONSOLE = "console"
    SMTP = "smtp"
    DISABLED = "disabled"


class SmtpSecurity(StrEnum):
    STARTTLS = "starttls"
    TLS = "tls"
    NONE = "none"


class ProcessRole(StrEnum):
    """Which process this is; in staging/production each needs only its own database URL."""

    ALL = "all"
    API = "api"
    WORKER = "worker"
    SCHEDULER = "scheduler"
    MIGRATE = "migrate"


SECURE_ENVIRONMENTS = frozenset({Environment.STAGING, Environment.PRODUCTION})
TLS_SSL_MODES = frozenset({"require", "verify-ca", "verify-full"})
MIN_TOKEN_HASH_KEY_LENGTH = 32


class Settings(BaseSettings):
    """Configuration loaded only from environment variables or a local ``.env`` file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="BELUNO_",
        extra="ignore",
        # Validation errors must not echo secrets (keys, keyrings, URLs) into logs.
        hide_input_in_errors=True,
    )

    environment: Environment = Environment.DEVELOPMENT
    # ``all`` (the default) requires every database URL; a deployment that gives each
    # process only its own credentials names the process here.
    process_role: ProcessRole = ProcessRole.ALL
    log_level: str = "INFO"
    release: str = "dev"
    api_database_url: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "api_database_url",
            "database_url",
            "BELUNO_API_DATABASE_URL",
            "BELUNO_DATABASE_URL",
        ),
    )
    worker_database_url: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("worker_database_url", "BELUNO_WORKER_DATABASE_URL"),
    )
    scheduler_database_url: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("scheduler_database_url", "BELUNO_SCHEDULER_DATABASE_URL"),
    )
    migration_database_url: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("migration_database_url", "BELUNO_MIGRATION_DATABASE_URL"),
    )
    sentry_dsn: SecretStr | None = None
    otel_exporter_otlp_endpoint: str | None = None
    api_max_request_bytes: int = Field(default=1_048_576, ge=1_024, le=10_485_760)

    # Identity: the API is its own token issuer (ADR 0007).
    auth_issuer: str = "beluno"
    auth_audience: str = "beluno-api"
    auth_signing_keys: SecretStr | None = None
    auth_access_token_ttl_seconds: int = Field(default=900, ge=60, le=3_600)
    auth_session_idle_ttl_seconds: int = Field(default=90 * 86_400, ge=3_600)
    auth_session_max_ttl_seconds: int = Field(default=180 * 86_400, ge=3_600)
    auth_refresh_reuse_grace_seconds: int = Field(default=30, ge=0, le=300)
    auth_step_up_max_age_seconds: int = Field(default=600, ge=60, le=3_600)
    auth_google_client_ids: list[str] = Field(default_factory=list)
    auth_apple_client_ids: list[str] = Field(default_factory=list)
    auth_magic_link_url: str | None = None
    # WebAuthn relying party: the app's domain (iOS associated domains, Android asset
    # links) and every origin a passkey response may come from (https://... or
    # android:apk-key-hash:...).
    webauthn_rp_id: str = "localhost"
    webauthn_rp_name: str = "Beluno"
    webauthn_origins: list[str] = Field(default_factory=lambda: ["http://localhost"])
    # Media: S3-compatible storage (RustFS self-hosted). The worker reaches it at
    # storage_endpoint_url; apps upload and download through presigned URLs signed for
    # storage_public_url (the address they can reach; defaults to the endpoint).
    storage_endpoint_url: str | None = None
    storage_public_url: str | None = None
    storage_region: str = "us-east-1"
    storage_bucket: str = "beluno-media"
    storage_access_key_id: SecretStr | None = None
    storage_secret_access_key: SecretStr | None = None
    # ClamAV daemon the worker streams every upload to before it is used.
    clamd_host: str | None = None
    clamd_port: int = Field(default=3310, ge=1, le=65_535)
    media_receipt_max_bytes: int = Field(default=15 * 1024 * 1024, ge=1024)
    # Below clamd's default StreamMaxLength (25M): every file is scanned whole.
    media_image_max_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)
    # Free-plan receipt limit per plan; unset until paid plans exist.
    media_receipts_per_plan: int | None = Field(default=None, ge=1)
    token_hash_key: SecretStr | None = None
    # Keyring for secrets kept at rest (booking codes and notes); see beluno.secret_box.
    booking_keys: SecretStr | None = None

    email_backend: EmailBackend = EmailBackend.CONSOLE
    email_from: str | None = None
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65_535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_security: SmtpSecurity = SmtpSecurity.STARTTLS
    smtp_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    # Kill switches keep reads and recovery available while new entrypoints are disabled.
    invites_enabled: bool = True
    guest_access_enabled: bool = True
    participant_claims_enabled: bool = True
    # Kill switch for every financial write; reads and exports stay available.
    finance_writes_enabled: bool = True
    sync_push_enabled: bool = True
    sync_pull_enabled: bool = True
    # Command names (for example ``plan.duplicate``) refused on REST and push alike.
    sync_disabled_commands: list[str] = Field(default_factory=list)

    # Sync kernel: offline support window and the retention that must outlast it.
    sync_offline_window_days: int = Field(default=90, ge=1, le=365)
    sync_change_retention_days: int = Field(default=180, ge=1, le=3_650)
    sync_operation_retention_days: int = Field(default=180, ge=1, le=3_650)
    # A plan whose deletion was scheduled is purged for good this many days later; it
    # can be restored until then. The database refuses anything under seven days.
    plan_purge_after_days: int = Field(default=30, ge=7, le=3_650)
    sync_push_max_operations: int = Field(default=100, ge=1, le=1_000)
    sync_push_max_bytes: int = Field(default=1_048_576, ge=4_096, le=10_485_760)
    # Bounded by the pull contract's maximum page size (500) plus the lookahead row.
    sync_pull_page_size: int = Field(default=200, ge=10, le=500)

    @field_validator(
        "api_database_url",
        "worker_database_url",
        "scheduler_database_url",
        "migration_database_url",
    )
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return value
        url = value.get_secret_value()
        if not url.startswith(("postgresql+psycopg://", "postgresql://")):
            raise ValueError("database URL must use a PostgreSQL Psycopg URL")
        return value

    @field_validator(
        "auth_signing_keys",
        "token_hash_key",
        "booking_keys",
        "smtp_password",
        "sentry_dsn",
        "auth_magic_link_url",
        "email_from",
        "smtp_host",
        "smtp_username",
        mode="before",
    )
    @classmethod
    def blank_means_unset(cls, value: object) -> object:
        """``KEY=`` in an env file means "not configured", not an empty secret."""

        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("token_hash_key")
    @classmethod
    def validate_token_hash_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < MIN_TOKEN_HASH_KEY_LENGTH:
            raise ValueError("token hash key must contain at least 32 characters")
        return value

    @field_validator("booking_keys")
    @classmethod
    def validate_booking_keys(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            from beluno.secret_box import SecretBox

            SecretBox.from_json(value.get_secret_value())
        return value

    @property
    def api_database_dsn(self) -> str | None:
        return self._secret_value(self.api_database_url)

    @property
    def worker_database_dsn(self) -> str | None:
        return self._secret_value(self.worker_database_url) or self.api_database_dsn

    @property
    def scheduler_database_dsn(self) -> str | None:
        return self._secret_value(self.scheduler_database_url) or self.api_database_dsn

    @property
    def migration_database_dsn(self) -> str | None:
        return self._secret_value(self.migration_database_url) or self.api_database_dsn

    @property
    def database_dsn(self) -> str | None:
        """Backward-compatible alias for the API runtime DSN."""

        return self.api_database_dsn

    @property
    def auth_is_configured(self) -> bool:
        return self.auth_signing_keys is not None and self.token_hash_key is not None

    def assert_runtime_requirements(self) -> None:
        """Fail closed in staging/production without leaking secret values."""

        if self.environment not in SECURE_ENVIRONMENTS:
            return

        dsn_names = {
            ProcessRole.API: ("BELUNO_API_DATABASE_URL", self.api_database_dsn),
            ProcessRole.WORKER: (
                "BELUNO_WORKER_DATABASE_URL",
                self._secret_value(self.worker_database_url),
            ),
            ProcessRole.SCHEDULER: (
                "BELUNO_SCHEDULER_DATABASE_URL",
                self._secret_value(self.scheduler_database_url),
            ),
            ProcessRole.MIGRATE: (
                "BELUNO_MIGRATION_DATABASE_URL",
                self._secret_value(self.migration_database_url),
            ),
        }
        required_dsn_names = dict(
            dsn_names.values()
            if self.process_role is ProcessRole.ALL
            else [dsn_names[self.process_role]]
        )
        missing = [name for name, value in required_dsn_names.items() if value is None]
        # Migrations and the heartbeat scheduler issue no tokens and send no email.
        serves_people = self.process_role in (
            ProcessRole.ALL,
            ProcessRole.API,
            ProcessRole.WORKER,
        )
        if serves_people and self.auth_signing_keys is None:
            missing.append("BELUNO_AUTH_SIGNING_KEYS")
        if serves_people and self.token_hash_key is None:
            missing.append("BELUNO_TOKEN_HASH_KEY")
        if self.process_role in (ProcessRole.ALL, ProcessRole.API) and self.booking_keys is None:
            missing.append("BELUNO_BOOKING_KEYS")
        if serves_people and self.email_backend is EmailBackend.SMTP:
            if not self.smtp_host:
                missing.append("BELUNO_SMTP_HOST")
            if not self.email_from:
                missing.append("BELUNO_EMAIL_FROM")
        if missing:
            raise RuntimeError(f"Missing secure-environment configuration: {', '.join(missing)}")

        for name, dsn in required_dsn_names.items():
            assert dsn is not None
            self._assert_tls_database_url(name, dsn)
        if serves_people and self.email_backend is EmailBackend.CONSOLE:
            raise RuntimeError("BELUNO_EMAIL_BACKEND=console is only allowed outside staging")
        if (
            serves_people
            and self.email_backend is EmailBackend.SMTP
            and self.smtp_security is SmtpSecurity.NONE
        ):
            raise RuntimeError("BELUNO_SMTP_SECURITY must use starttls or tls")
        if self.process_role in (ProcessRole.ALL, ProcessRole.API):
            self._assert_webauthn_party()
        if self.process_role in (ProcessRole.ALL, ProcessRole.API, ProcessRole.WORKER):
            self._assert_storage()
        if self.auth_magic_link_url is not None:
            self._assert_https_url("BELUNO_AUTH_MAGIC_LINK_URL", self.auth_magic_link_url)
        if self.otel_exporter_otlp_endpoint is not None:
            self._assert_https_url(
                "BELUNO_OTEL_EXPORTER_OTLP_ENDPOINT",
                self.otel_exporter_otlp_endpoint,
            )
        self.assert_retention_requirements()

    def assert_retention_requirements(self) -> None:
        """Retention must outlast the offline window or replays and cursors break."""

        shortest = min(self.sync_change_retention_days, self.sync_operation_retention_days)
        if shortest < self.sync_offline_window_days:
            raise RuntimeError(
                "BELUNO_SYNC_CHANGE_RETENTION_DAYS and BELUNO_SYNC_OPERATION_RETENTION_DAYS "
                "must be at least BELUNO_SYNC_OFFLINE_WINDOW_DAYS"
            )

    def assert_production_requirements(self) -> None:
        """Compatibility alias for callers that previously used this method."""

        self.assert_runtime_requirements()

    def _assert_storage(self) -> None:
        missing = [
            name
            for name, value in (
                ("BELUNO_STORAGE_ENDPOINT_URL", self.storage_endpoint_url),
                ("BELUNO_STORAGE_ACCESS_KEY_ID", self.storage_access_key_id),
                ("BELUNO_STORAGE_SECRET_ACCESS_KEY", self.storage_secret_access_key),
            )
            if not value
        ]
        if self.process_role in (ProcessRole.ALL, ProcessRole.WORKER) and not self.clamd_host:
            missing.append("BELUNO_CLAMD_HOST")
        if missing:
            raise RuntimeError(f"Missing secure-environment configuration: {', '.join(missing)}")
        # Apps fetch presigned URLs over the internet: only over TLS.
        self._assert_https_url(
            "BELUNO_STORAGE_PUBLIC_URL", self.storage_public_url or self.storage_endpoint_url
        )

    def _assert_webauthn_party(self) -> None:
        """A real domain, and origins that are that domain (or a subdomain) over https,
        or the signed Android app."""

        rp_id = self.webauthn_rp_id
        if (
            not rp_id
            or rp_id != rp_id.lower()
            or rp_id in ("localhost",)
            or "." not in rp_id
            or any(character in rp_id for character in ":/@ ")
            or rp_id.replace(".", "").isdigit()
        ):
            raise RuntimeError("BELUNO_WEBAUTHN_RP_ID must be the app's domain")
        if not self.webauthn_origins:
            raise RuntimeError("BELUNO_WEBAUTHN_ORIGINS must list the app's origins")
        for origin in self.webauthn_origins:
            if origin.startswith("android:apk-key-hash:"):
                continue
            host = urlsplit(origin).hostname if origin.startswith("https://") else None
            if host is None or not (host == rp_id or host.endswith(f".{rp_id}")):
                raise RuntimeError(
                    "BELUNO_WEBAUTHN_ORIGINS must be https:// origins on the relying party's "
                    "domain or android:apk-key-hash: origins"
                )

    @staticmethod
    def _secret_value(value: SecretStr | None) -> str | None:
        return value.get_secret_value() if value else None

    @staticmethod
    def _assert_tls_database_url(name: str, value: str) -> None:
        parameters = parse_qs(urlparse(value).query)
        sslmode = parameters.get("sslmode", [""])[0]
        if sslmode not in TLS_SSL_MODES:
            raise RuntimeError(f"{name} must set sslmode to require, verify-ca, or verify-full")

    @staticmethod
    def _assert_https_url(name: str, value: str | None) -> None:
        if value is None or urlparse(value).scheme != "https":
            raise RuntimeError(f"{name} must use https")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.assert_runtime_requirements()
    return settings
