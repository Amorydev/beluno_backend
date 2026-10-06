"""Direct superuser access for test setup and assertions that bypass the API."""

from __future__ import annotations

from typing import Any

import psycopg

# Every application table, so a suite can reset state between tests or examples.
TRUNCATE_ALL_SQL = """
TRUNCATE
    sync_audit.audit_events, sync_audit.change_log, sync_audit.scope_heads,
    sync_audit.operations,
    plans.travel_segments, plans.travel_plan_details, plans.plan_invites, groups.group_invites,
    plans.plan_participants, plans.plans, plans.plan_series,
    groups.group_memberships, groups.groups,
    iam.rate_limit_counters, iam.email_challenges, iam.refresh_tokens, iam.sessions,
    iam.user_identities, iam.users,
    jobs.procrastinate_events, jobs.procrastinate_jobs
CASCADE
"""


class AdminDatabase:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def execute(self, statement: str, *params: object) -> None:
        with psycopg.connect(self._dsn, autocommit=True) as connection:
            connection.execute(statement, params)

    def fetch(self, statement: str, *params: object) -> list[tuple[Any, ...]]:
        with psycopg.connect(self._dsn) as connection:
            return list(connection.execute(statement, params).fetchall())

    def scalar(self, statement: str, *params: object) -> Any:
        rows = self.fetch(statement, *params)
        return rows[0][0] if rows else None

    def truncate_all(self) -> None:
        self.execute(TRUNCATE_ALL_SQL)
