"""Direct superuser access for test setup and assertions that bypass the API."""

from __future__ import annotations

from typing import Any

import psycopg

# Every application table except seeded reference data (``finance.currencies``), so a
# suite can reset state between tests or examples.
TRUNCATE_ALL_SQL = """
TRUNCATE
    finance.ledger_postings, finance.ledger_transactions, finance.fund_movements,
    finance.fund_settings, finance.refund_shares, finance.expense_refunds,
    finance.expense_splits, finance.expense_payers, finance.expense_revisions,
    finance.expenses, finance.cost_commitments, finance.settlements, finance.budgets,
    finance.fx_snapshots, finance.account_balances, finance.ledger_accounts,
    finance.ledger_confirmations, finance.fund_counts, finance.market_rates,
    finance.plan_ledger_heads,
    sync_audit.audit_events, sync_audit.change_log, sync_audit.scope_heads,
    sync_audit.operations,
    decisions.poll_outcomes, decisions.poll_results, decisions.poll_votes,
    decisions.poll_electorate, decisions.poll_options, decisions.polls,
    schedule_places.item_attendance, schedule_places.itinerary_items,
    schedule_places.place_reactions, schedule_places.places,
    activity.events, people.crews, plans.plan_invites, plans.plan_participants, plans.plans,
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
