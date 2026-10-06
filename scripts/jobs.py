"""Operator CLI for the job queue: list dead letters, replay one, show health.

Runs with the worker database role (BELUNO_WORKER_DATABASE_URL). Every replay
is recorded as an audit event naming the operator.

    uv run python scripts/jobs.py list [--status failed] [--limit 50]
    uv run python scripts/jobs.py retry JOB_ID --operator NAME
    uv run python scripts/jobs.py health
"""

from __future__ import annotations

import argparse
import asyncio
import json

from beluno.worker.deadletter import list_jobs, queue_health, retry_job
from beluno.worker.runtime import get_worker_runtime


async def run(arguments: argparse.Namespace) -> int:
    runtime = get_worker_runtime()
    try:
        if arguments.command == "list":
            for job in await list_jobs(runtime, status=arguments.status, limit=arguments.limit):
                print(
                    f"{job.id}\t{job.status}\t{job.queue_name}\t{job.task_name}\t"
                    f"attempts={job.attempts}\t{json.dumps(job.args, sort_keys=True)}"
                )
            return 0
        if arguments.command == "retry":
            job = await retry_job(runtime, arguments.job_id, operator=arguments.operator)
            print(f"{job.id}\t{job.status}\tattempts={job.attempts}")
            return 0
        health = await queue_health(runtime)
        print(json.dumps(health.as_measurements(), sort_keys=True))
        return 0
    finally:
        await runtime.database.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list jobs by status (default: failed)")
    listing.add_argument("--status", default="failed")
    listing.add_argument("--limit", type=int, default=50)
    retry = commands.add_parser("retry", help="put a failed job back on the queue")
    retry.add_argument("job_id", type=int)
    retry.add_argument("--operator", required=True, help="who is replaying (audited)")
    commands.add_parser("health", help="queue depths and oldest waiting age")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
