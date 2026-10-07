"""Database drills: backup/restore, migration rehearsal, and local provisioning.

Every drill works on scratch copies and never writes to the source database.

    # backup -> restore -> bump sync generations -> reconcile every ledger, with timings
    uv run python scripts/db_drill.py backup-restore --source-db beluno

    # restore a dump (or a fresh dump of the source), upgrade the copy to head, validate
    uv run python scripts/db_drill.py rehearse-migration --source-db beluno [--dump FILE]

    # disposable cluster only: create the runtime roles, a database, and migrate it
    uv run python scripts/db_drill.py provision --database beluno_load [--revision REV]

DSNs come from the CLI or the environment and are never printed:

    --admin-dsn      BELUNO_DRILL_ADMIN_DSN         superuser (or equivalent): dump, create, restore
    --migrator-dsn   BELUNO_MIGRATION_DATABASE_URL  migrator role: bump generations, alembic
    --worker-dsn     BELUNO_WORKER_DATABASE_URL     worker role: ledger reconciliation

The database name in each DSN is replaced by the database being worked on, so
the DSNs may point at any database of the cluster.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse, urlunparse

import psycopg
from psycopg import sql

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RECONCILE_PAGE_SIZE = 200
# Application schemas hold every table a restore must bring back.
COUNT_TABLES_SQL = """
SELECT n.nspname, c.relname
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
ORDER BY 1, 2
"""
RUNTIME_ROLES = ("migrator", "api_runtime", "worker_runtime", "scheduler_runtime")


# --- DSN helpers ----------------------------------------------------------------


def plain_dsn(dsn: str) -> str:
    """psycopg and the PostgreSQL client tools want ``postgresql://``, not the SQLAlchemy form."""

    return dsn.replace("postgresql+psycopg://", "postgresql://", 1)


def with_database(dsn: str, database: str) -> str:
    parsed = urlparse(plain_dsn(dsn))
    return urlunparse(parsed._replace(path=f"/{database}"))


def database_of(dsn: str) -> str:
    name = urlparse(plain_dsn(dsn)).path.lstrip("/")
    if not name:
        raise SystemExit("the DSN names no database; pass --source-db")
    return name


def tool_environment(dsn: str) -> dict[str, str]:
    """libpq variables for a DSN so passwords never appear in a process listing."""

    parsed = urlparse(plain_dsn(dsn))
    variables = {
        "PGHOST": parsed.hostname or "localhost",
        "PGPORT": str(parsed.port or 5432),
        "PGDATABASE": parsed.path.lstrip("/"),
    }
    if parsed.username:
        variables["PGUSER"] = unquote(parsed.username)
    if parsed.password:
        variables["PGPASSWORD"] = unquote(parsed.password)
    sslmode = parse_qs(parsed.query).get("sslmode")
    if sslmode:
        variables["PGSSLMODE"] = sslmode[0]
    return variables


def run_tool(arguments: list[str], dsn: str) -> None:
    result = subprocess.run(
        arguments,
        env={**os.environ, **tool_environment(dsn)},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"{arguments[0]} failed ({result.returncode}):\n{result.stderr.strip()}")


# --- Timings --------------------------------------------------------------------


@dataclass
class Timings:
    steps: list[tuple[str, float]] = field(default_factory=list)

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - started
            self.steps.append((name, elapsed))
            print(f"  {name}: {elapsed:.2f}s", flush=True)

    def report(self) -> None:
        print("\nTimings")
        for name, elapsed in self.steps:
            print(f"  {name:<28}{elapsed:>9.2f}s")
        print(f"  {'total':<28}{sum(elapsed for _, elapsed in self.steps):>9.2f}s")


# --- Database operations --------------------------------------------------------


def provision_roles(admin_dsn: str) -> None:
    """Create the runtime roles with synthetic passwords. Disposable clusters only."""

    from beluno.testkit.environment import RUNTIME_PASSWORDS

    with psycopg.connect(plain_dsn(admin_dsn), autocommit=True) as connection:
        for role, password in RUNTIME_PASSWORDS.items():
            exists = connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            verb = "ALTER" if exists.fetchone() else "CREATE"
            connection.execute(
                sql.SQL(
                    verb + " ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(role), sql.Literal(password))
            )
        for role in ("support_readonly", "restore_validator"):
            if not connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
            ).fetchone():
                connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))


def create_database(admin_dsn: str, name: str) -> None:
    """Empty database with the connect/create grants the runtime roles expect."""

    identifier = sql.Identifier(name)
    with psycopg.connect(plain_dsn(admin_dsn), autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(identifier))
    with psycopg.connect(with_database(admin_dsn, name), autocommit=True) as connection:
        connection.execute(
            sql.SQL("REVOKE CREATE, TEMPORARY ON DATABASE {} FROM PUBLIC").format(identifier)
        )
        roles = sql.SQL(", ").join(sql.Identifier(role) for role in RUNTIME_ROLES)
        connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(identifier, roles))
        connection.execute(sql.SQL("GRANT CREATE ON DATABASE {} TO migrator").format(identifier))
        connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        connection.execute("GRANT USAGE, CREATE ON SCHEMA public TO migrator")


def drop_database(admin_dsn: str, name: str) -> None:
    with psycopg.connect(plain_dsn(admin_dsn), autocommit=True) as connection:
        connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (name,)
        )
        connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def dump_database(admin_dsn: str, database: str, target: Path) -> None:
    # Superuser dump: tables use forced row-level security, which an ordinary role cannot read.
    run_tool(
        ["pg_dump", "--format=custom", "--no-password", f"--file={target}"],
        with_database(admin_dsn, database),
    )


def restore_database(admin_dsn: str, database: str, dump: Path) -> None:
    """Restore with owners and grants intact: the migrator keeps owning every object."""

    run_tool(
        [
            "pg_restore",
            "--no-password",
            "--exit-on-error",
            "--single-transaction",
            f"--dbname={database}",
            str(dump),
        ],
        with_database(admin_dsn, database),
    )


def table_counts(admin_dsn: str, database: str) -> dict[str, int]:
    """Exact row count per ``schema.table``, read as superuser (bypasses row-level security)."""

    counts: dict[str, int] = {}
    with psycopg.connect(with_database(admin_dsn, database), autocommit=True) as connection:
        for schema, table in connection.execute(COUNT_TABLES_SQL).fetchall():
            query = sql.SQL("SELECT count(*) FROM {}.{}").format(
                sql.Identifier(schema), sql.Identifier(table)
            )
            row = connection.execute(query).fetchone()
            counts[f"{schema}.{table}"] = int(row[0]) if row else 0
    return counts


def schema_totals(counts: dict[str, int]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for name, value in counts.items():
        schema = name.split(".", 1)[0]
        totals[schema] = totals.get(schema, 0) + value
    return totals


def bump_generations(migrator_dsn: str, database: str) -> int:
    """Runbook step: every device must resync after a restore."""

    with psycopg.connect(with_database(migrator_dsn, database)) as connection:
        cursor = connection.execute("UPDATE sync_audit.scope_heads SET generation = generation + 1")
        return cursor.rowcount


def reconcile_all(worker_dsn: str, database: str) -> tuple[int, int]:
    """Reconcile every plan ledger with the worker role: ``(plans, findings)``."""

    plans = findings = 0
    after: str | None = None
    with psycopg.connect(with_database(worker_dsn, database)) as connection:
        while True:
            ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT finance.ledger_plan_ids(%s::uuid, %s)", (after, RECONCILE_PAGE_SIZE)
                ).fetchall()
            ]
            if not ids:
                return plans, findings
            for plan_id in ids:
                rows = connection.execute(
                    "SELECT problem, account_id FROM finance.reconcile_plan(%s::uuid)", (plan_id,)
                ).fetchall()
                plans += 1
                findings += len(rows)
                for problem, account_id in rows:
                    print(f"  DRIFT plan={plan_id} {problem} account={account_id}")
            after = ids[-1]
            connection.rollback()


def alembic_revision(admin_dsn: str, database: str) -> str:
    with psycopg.connect(with_database(admin_dsn, database), autocommit=True) as connection:
        row = connection.execute("SELECT version_num FROM public.alembic_version").fetchone()
    return str(row[0]) if row else "none"


def migrate_database(migrator_dsn: str, database: str, revision: str | None = None) -> None:
    """Migrate one database under the migrator role (the environment pins the target).

    Default is the deploy path, ``scripts/bootstrap_database.py``: migrations to head, the
    queue schema, and the runtime grants. With ``revision``, plain alembic stops there, which
    builds an older copy to rehearse an upgrade from.
    """

    environment = {
        **os.environ,
        "BELUNO_MIGRATION_DATABASE_URL": with_database(migrator_dsn, database).replace(
            "postgresql://", "postgresql+psycopg://", 1
        ),
    }
    command = (
        [sys.executable, "-m", "alembic", "upgrade", revision]
        if revision
        else [sys.executable, str(PROJECT_ROOT / "scripts" / "bootstrap_database.py")]
    )
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"migration failed:\n{result.stderr.strip()[-2000:]}")


def structure_signals(admin_dsn: str, database: str) -> dict[str, int]:
    """Catalog facts a migration must not weaken: RLS, forced RLS, validated constraints."""

    queries = {
        "tables_with_rls": "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = "
        "c.relnamespace WHERE c.relrowsecurity AND n.nspname NOT LIKE 'pg\\_%'",
        "tables_with_forced_rls": "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = "
        "c.relnamespace WHERE c.relforcerowsecurity AND n.nspname NOT LIKE 'pg\\_%'",
        "rls_policies": "SELECT count(*) FROM pg_policies",
        "unvalidated_constraints": "SELECT count(*) FROM pg_constraint WHERE NOT convalidated",
        "invalid_indexes": "SELECT count(*) FROM pg_index WHERE NOT indisvalid",
    }
    with psycopg.connect(with_database(admin_dsn, database), autocommit=True) as connection:
        return {
            name: int((connection.execute(query).fetchone() or (0,))[0])
            for name, query in queries.items()
        }


# --- Drills ---------------------------------------------------------------------


def print_counts(label: str, counts: dict[str, int]) -> None:
    totals = schema_totals(counts)
    print(f"{label}: {sum(counts.values())} rows in {len(counts)} tables")
    for schema, total in totals.items():
        print(f"    {schema:<18}{total:>10}")


def compare_counts(before: dict[str, int], after: dict[str, int], *, exact: bool) -> list[str]:
    problems = []
    for table, value in before.items():
        restored = after.get(table)
        if restored is None:
            problems.append(f"{table}: missing after")
        elif (exact and restored != value) or restored < value:
            problems.append(f"{table}: {value} -> {restored}")
    return problems


@dataclass
class Scratch:
    """A scratch database plus an optional dump, removed on exit unless kept."""

    admin_dsn: str
    keep: bool
    names: list[str] = field(default_factory=list)
    workdir: Path | None = None

    def database(self, source: str, purpose: str) -> str:
        name = f"{source}_{purpose}_{secrets.token_hex(3)}"
        if name == source:
            raise SystemExit("refusing to use the source database as a scratch target")
        create_database(self.admin_dsn, name)
        self.names.append(name)
        return name

    def directory(self) -> Path:
        if self.workdir is None:
            self.workdir = Path(tempfile.mkdtemp(prefix="beluno-drill-"))
        return self.workdir

    def cleanup(self) -> None:
        if self.workdir is not None:
            # The dump holds production data: always remove it unless the operator keeps it.
            if self.keep:
                print(f"kept dump directory: {self.workdir}")
            else:
                shutil.rmtree(self.workdir, ignore_errors=True)
        for name in self.names:
            if self.keep:
                print(f"kept database: {name}")
            else:
                drop_database(self.admin_dsn, name)


def backup_restore(arguments: argparse.Namespace) -> int:
    timings = Timings()
    scratch = Scratch(arguments.admin_dsn, arguments.keep)
    try:
        print(f"backup-restore drill on database {arguments.source_db}")
        with timings.step("count source rows"):
            source_counts = table_counts(arguments.admin_dsn, arguments.source_db)
        print_counts("source", source_counts)
        dump = scratch.directory() / "source.dump"
        with timings.step("pg_dump -Fc"):
            dump_database(arguments.admin_dsn, arguments.source_db, dump)
        size_mb = dump.stat().st_size / 1_048_576
        print(f"  dump size: {size_mb:.2f} MiB")
        with timings.step("create database"):
            target = scratch.database(arguments.source_db, "restore")
        with timings.step("pg_restore"):
            restore_database(arguments.admin_dsn, target, dump)
        with timings.step("bump sync generations"):
            bumped = bump_generations(arguments.migrator_dsn, target)
        print(f"  scope heads bumped: {bumped}")
        with timings.step("reconcile ledgers"):
            plans, findings = reconcile_all(arguments.worker_dsn, target)
        print(f"  plans reconciled: {plans}, findings: {findings}")
        with timings.step("count restored rows"):
            restored_counts = table_counts(arguments.admin_dsn, target)
        problems = compare_counts(source_counts, restored_counts, exact=True)
        for problem in problems:
            print(f"  ROW COUNT MISMATCH {problem}")
        timings.report()
        print(
            f"\nresult: {'FAIL' if problems or findings else 'OK'} "
            f"(dump {size_mb:.2f} MiB, {sum(source_counts.values())} rows)"
        )
        return 1 if problems or findings else 0
    finally:
        scratch.cleanup()


def rehearse_migration(arguments: argparse.Namespace) -> int:
    timings = Timings()
    scratch = Scratch(arguments.admin_dsn, arguments.keep)
    try:
        print(f"migration rehearsal using {arguments.dump or arguments.source_db}")
        if arguments.dump:
            dump = Path(arguments.dump)
        else:
            dump = scratch.directory() / "source.dump"
            with timings.step("pg_dump -Fc"):
                dump_database(arguments.admin_dsn, arguments.source_db, dump)
        with timings.step("create database"):
            copy = scratch.database(arguments.source_db, "rehearsal")
        with timings.step("pg_restore"):
            restore_database(arguments.admin_dsn, copy, dump)
        revision_before = alembic_revision(arguments.admin_dsn, copy)
        counts_before = table_counts(arguments.admin_dsn, copy)
        signals_before = structure_signals(arguments.admin_dsn, copy)
        print(f"revision before: {revision_before}")
        print_counts("before", counts_before)
        with timings.step("migrate to head"):
            migrate_database(arguments.migrator_dsn, copy)
        revision_after = alembic_revision(arguments.admin_dsn, copy)
        with timings.step("validate"):
            counts_after = table_counts(arguments.admin_dsn, copy)
            signals_after = structure_signals(arguments.admin_dsn, copy)
            plans, findings = reconcile_all(arguments.worker_dsn, copy)
        print(f"revision after: {revision_after}")
        print_counts("after", counts_after)
        print(f"  plans reconciled: {plans}, findings: {findings}")
        problems = compare_counts(counts_before, counts_after, exact=False)
        for name, value in signals_after.items():
            print(f"  {name}: {signals_before[name]} -> {value}")
        weakened = [
            name
            for name in ("tables_with_rls", "tables_with_forced_rls", "rls_policies")
            if signals_after[name] < signals_before[name]
        ]
        broken = [
            name
            for name in ("unvalidated_constraints", "invalid_indexes")
            if signals_after[name] > signals_before[name]
        ]
        problems += [f"{name} decreased" for name in weakened] + [f"{name} grew" for name in broken]
        for problem in problems:
            print(f"  VALIDATION FAILED {problem}")
        timings.report()
        failed = bool(problems or findings)
        print(f"\nresult: {'FAIL' if failed else 'OK'} ({revision_before} -> {revision_after})")
        return 1 if failed else 0
    finally:
        scratch.cleanup()


def provision(arguments: argparse.Namespace) -> int:
    """Migrated database with the runtime roles on a disposable cluster (load and drills)."""

    from beluno.testkit.environment import role_dsn

    provision_roles(arguments.admin_dsn)
    create_database(arguments.admin_dsn, arguments.database)
    admin = with_database(arguments.admin_dsn, arguments.database)
    revision = None if arguments.revision == "head" else arguments.revision
    migrate_database(role_dsn(admin, "migrator"), arguments.database, revision)
    reached = alembic_revision(admin, arguments.database)
    print(f"provisioned database {arguments.database} at {reached}")
    return 0


def drop(arguments: argparse.Namespace) -> int:
    drop_database(arguments.admin_dsn, arguments.database)
    print(f"dropped database {arguments.database}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="mode", required=True)

    def common(command: argparse.ArgumentParser, *, drill: bool = True) -> None:
        command.add_argument("--admin-dsn", default=os.environ.get("BELUNO_DRILL_ADMIN_DSN"))
        if drill:
            command.add_argument(
                "--migrator-dsn", default=os.environ.get("BELUNO_MIGRATION_DATABASE_URL")
            )
            command.add_argument(
                "--worker-dsn", default=os.environ.get("BELUNO_WORKER_DATABASE_URL")
            )
            command.add_argument("--source-db", help="database to back up (default: from the DSN)")
            command.add_argument(
                "--keep", action="store_true", help="keep scratch databases and dumps"
            )

    restore = commands.add_parser("backup-restore", help="dump, restore, bump, reconcile")
    common(restore)
    restore.set_defaults(handler=backup_restore)
    rehearsal = commands.add_parser("rehearse-migration", help="upgrade a restored copy to head")
    common(rehearsal)
    rehearsal.add_argument("--dump", help="restore this pg_dump -Fc file instead of dumping")
    rehearsal.set_defaults(handler=rehearse_migration)
    provisioner = commands.add_parser("provision", help="roles + migrated database (disposable)")
    common(provisioner, drill=False)
    provisioner.add_argument("--database", required=True)
    provisioner.add_argument(
        "--revision", default="head", help="migrate only this far (to rehearse an upgrade)"
    )
    provisioner.set_defaults(handler=provision)
    dropper = commands.add_parser("drop", help="drop a scratch database")
    common(dropper, drill=False)
    dropper.add_argument("--database", required=True)
    dropper.set_defaults(handler=drop)
    return parser


def main() -> int:
    parser = build_parser()
    arguments = parser.parse_args()
    if not arguments.admin_dsn:
        parser.error("--admin-dsn or BELUNO_DRILL_ADMIN_DSN is required")
    if arguments.mode in ("backup-restore", "rehearse-migration"):
        for name in ("migrator_dsn", "worker_dsn"):
            if not getattr(arguments, name):
                parser.error(f"--{name.replace('_', '-')} (or its BELUNO_ variable) is required")
        arguments.source_db = arguments.source_db or database_of(arguments.migrator_dsn)
    handler = arguments.handler
    return int(handler(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
