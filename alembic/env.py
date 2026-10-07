"""Alembic environment for reviewed PostgreSQL migrations."""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from beluno.config import Settings

config = context.config

if config.config_file_name is not None:
    # Keep loggers that already exist: running migrations must not silence the app's logs.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Domain tables are introduced by reviewed migrations. Metadata is intentionally
# not the production DDL source for RLS, triggers, functions, or grants.
target_metadata = None


def get_url() -> str:
    settings = Settings()
    if settings.migration_database_dsn is not None:
        return settings.migration_database_dsn
    configured_url = config.get_main_option("sqlalchemy.url")
    if configured_url and "placeholder" not in configured_url:
        return configured_url
    raise RuntimeError("BELUNO_MIGRATION_DATABASE_URL is required for migrations")


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
