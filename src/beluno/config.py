"""Typed configuration and environment safety checks."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from urllib.parse import parse_qs, urlparse

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


SECURE_ENVIRONMENTS = frozenset({Environment.STAGING, Environment.PRODUCTION})
TLS_SSL_MODES = frozenset({"require", "verify-ca", "verify-full"})
MIN_TOKEN_HASH_KEY_LENGTH = 32


class Settings(BaseSettings):
    """Configuration loaded only from environment variables or a local ``.env`` file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="BELUNO_",
        extra="ignore",
    )

    environment: Environment = Environment.DEVELOPMENT
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
    token_hash_key: SecretStr | None = None

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
    sync_push_enabled: bool = True
    sync_pull_enabled: bool = True
    # Command names (for example ``plan.duplicate``) refused on REST and push alike.
    sync_disabled_commands: list[str] = Field(default_factory=list)

    # Sync kernel: offline support window and the retention that must outlast it.
    sync_offline_window_days: int = Field(default=90, ge=1, le=365)
    sync_change_retention_days: int = Field(default=180, ge=1, le=3_650)
    sync_operation_retention_days: int = Field(default=180, ge=1, le=3_650)
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

        required_dsn_names = {
            "BELUNO_API_DATABASE_URL": self.api_database_dsn,
            "BELUNO_WORKER_DATABASE_URL": self._secret_value(self.worker_database_url),
            "BELUNO_SCHEDULER_DATABASE_URL": self._secret_value(self.scheduler_database_url),
            "BELUNO_MIGRATION_DATABASE_URL": self._secret_value(self.migration_database_url),
        }
        missing = [name for name, value in required_dsn_names.items() if value is None]
        if self.auth_signing_keys is None:
            missing.append("BELUNO_AUTH_SIGNING_KEYS")
        if self.token_hash_key is None:
            missing.append("BELUNO_TOKEN_HASH_KEY")
        if self.email_backend is EmailBackend.SMTP:
            if not self.smtp_host:
                missing.append("BELUNO_SMTP_HOST")
            if not self.email_from:
                missing.append("BELUNO_EMAIL_FROM")
        if missing:
            raise RuntimeError(f"Missing secure-environment configuration: {', '.join(missing)}")

        for name, dsn in required_dsn_names.items():
            assert dsn is not None
            self._assert_tls_database_url(name, dsn)
        if self.email_backend is EmailBackend.CONSOLE:
            raise RuntimeError("BELUNO_EMAIL_BACKEND=console is only allowed outside staging")
        if self.email_backend is EmailBackend.SMTP and self.smtp_security is SmtpSecurity.NONE:
            raise RuntimeError("BELUNO_SMTP_SECURITY must use starttls or tls")
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
