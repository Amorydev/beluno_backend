#!/bin/sh
# Docker-only role bootstrap. Production uses the reviewed SQL plus managed
# secrets; these synthetic passwords must never leave local development.
set -eu

psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set migrator_password="$BELUNO_MIGRATOR_DB_PASSWORD" \
  --set api_password="$BELUNO_API_DB_PASSWORD" \
  --set worker_password="$BELUNO_WORKER_DB_PASSWORD" \
  --set scheduler_password="$BELUNO_SCHEDULER_DB_PASSWORD" <<'EOSQL'
CREATE ROLE migrator LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD :'migrator_password';
CREATE ROLE api_runtime LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD :'api_password';
CREATE ROLE worker_runtime LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD :'worker_password';
CREATE ROLE scheduler_runtime LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD :'scheduler_password';
CREATE ROLE support_readonly NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE restore_validator NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
REVOKE CREATE, TEMPORARY ON DATABASE beluno_test FROM PUBLIC;
GRANT CONNECT ON DATABASE beluno_test TO migrator, api_runtime, worker_runtime, scheduler_runtime, support_readonly, restore_validator;
GRANT CREATE ON DATABASE beluno_test TO migrator;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO migrator;
EOSQL
