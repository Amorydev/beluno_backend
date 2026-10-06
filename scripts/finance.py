"""Operator CLI for ledger health: reconcile plans and repair balance projections.

Runs with the worker database role (BELUNO_WORKER_DATABASE_URL). A rebuild only
recomputes balances from canonical postings; it never edits a posting. Applying
it is recorded as an audit event naming the operator.

    uv run python scripts/finance.py reconcile [--plan PLAN_ID]
    uv run python scripts/finance.py rebuild PLAN_ID [--apply --operator NAME]
"""

from __future__ import annotations

import argparse
import asyncio
from uuid import UUID

from beluno.modules.finance.maintenance import rebuild_balances, reconcile_ledgers, reconcile_plan
from beluno.worker.runtime import get_worker_runtime


async def run(arguments: argparse.Namespace) -> int:
    runtime = get_worker_runtime()
    try:
        if arguments.command == "reconcile":
            if arguments.plan is None:
                findings = await reconcile_ledgers(runtime)
                print(f"findings={findings}")
                return 1 if findings else 0
            drift = await reconcile_plan(runtime, UUID(arguments.plan))
            for item in drift:
                print(f"{item.problem}\taccount={item.account_id}")
            return 1 if drift else 0
        if arguments.apply and not arguments.operator:
            raise SystemExit("--apply needs --operator NAME (the repair is audited)")
        repairs = await rebuild_balances(
            runtime,
            UUID(arguments.plan_id),
            apply=arguments.apply,
            operator=arguments.operator or "",
        )
        for repair in repairs:
            print(
                f"{repair.account_id}\trecorded={repair.recorded_minor}\t"
                f"rebuilt={repair.rebuilt_minor}"
            )
        print(("applied" if arguments.apply else "dry run") + f": {len(repairs)} account(s)")
        return 0
    finally:
        await runtime.database.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    reconcile = commands.add_parser("reconcile", help="check one plan or every plan ledger")
    reconcile.add_argument("--plan")
    rebuild = commands.add_parser("rebuild", help="rebuild a plan's balances from postings")
    rebuild.add_argument("plan_id")
    rebuild.add_argument("--apply", action="store_true", help="write the rebuilt balances")
    rebuild.add_argument("--operator", help="who is repairing (audited)")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
