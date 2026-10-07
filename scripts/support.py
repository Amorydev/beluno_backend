"""Operator CLI for problem reports: list the newest, or show one by its code.

Runs with the worker database role (BELUNO_WORKER_DATABASE_URL). Reports hold the
person's own words and a snapshot of IDs, states, and versions; treat them as
personal data. Every report read is audited with the operator's name.

    uv run python scripts/support.py list --operator NAME [--limit 50]
    uv run python scripts/support.py show BLN-7F3K-29QD --operator NAME
"""

from __future__ import annotations

import argparse
import asyncio
import json

from beluno.modules.support import list_reports
from beluno.worker.runtime import get_worker_runtime


async def run(arguments: argparse.Namespace) -> int:
    runtime = get_worker_runtime()
    try:
        if arguments.command == "list":
            for report in await list_reports(
                runtime, operator=arguments.operator, limit=arguments.limit
            ):
                print(
                    f"{report.created_at.isoformat()}\t{report.id}\t{report.category}\t"
                    f"{report.diagnostic_code or '-'}\tplan={report.plan_id or '-'}\t"
                    f"{json.dumps(report.message, ensure_ascii=False)}"
                )
            return 0
        found = await list_reports(
            runtime, operator=arguments.operator, limit=1, code=arguments.code
        )
        if not found:
            print(f"no report has the code {arguments.code}")
            return 1
        report = found[0]
        print(
            json.dumps(
                {
                    "id": str(report.id),
                    "user_id": str(report.user_id),
                    "category": report.category,
                    "plan_id": str(report.plan_id) if report.plan_id else None,
                    "linked": {"type": report.entity_type, "id": str(report.entity_id)}
                    if report.entity_id
                    else None,
                    "message": report.message,
                    "diagnostic_code": report.diagnostic_code,
                    "diagnostics": report.diagnostics,
                    "created_at": report.created_at.isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        await runtime.database.close()


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="the newest reports")
    listing.add_argument("--limit", type=positive, default=50)
    listing.add_argument("--operator", required=True, help="who is reading (audited)")
    show = commands.add_parser("show", help="one report by its diagnostic code")
    show.add_argument("code")
    show.add_argument("--operator", required=True, help="who is reading (audited)")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
