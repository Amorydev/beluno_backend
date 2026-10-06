-- Execute once with a PostgreSQL administrator before the first migration.
-- This SQL creates disabled login roles; the deployment secret manager assigns
-- credentials separately. Never grant these roles BYPASSRLS or table ownership.

DO $$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'migrator',
        'api_runtime',
        'worker_runtime',
        'scheduler_runtime',
        'support_readonly',
        'restore_validator'
    ]
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format(
                'CREATE ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD NULL',
                role_name
            );
        END IF;
    END LOOP;
END;
$$;

DO $$
BEGIN
    EXECUTE format('REVOKE CREATE, TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO migrator, api_runtime, worker_runtime, scheduler_runtime, support_readonly, restore_validator',
        current_database()
    );
    -- The migrator owns DDL: module schemas and Alembic's version table.
    EXECUTE format('GRANT CREATE ON DATABASE %I TO migrator', current_database());
END;
$$;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO migrator;
