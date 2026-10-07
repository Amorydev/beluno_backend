"""Operator CLI: send news from the team to everyone who turned news on.

Runs with the worker database role (BELUNO_WORKER_DATABASE_URL). People get the
Vietnamese text when their profile's locale is Vietnamese, the English one otherwise;
the worker delivers it within a minute, outside quiet hours. A key is sent once:
running the same key again reaches nobody twice. Every send is audited.

    uv run python scripts/news.py --key trip-pass-launch --operator NAME \\
        --title-vi "..." --body-vi "..." --title-en "..." --body-en "..."
"""

from __future__ import annotations

import argparse
import asyncio

from beluno.modules.notifications import News, send_news
from beluno.worker.runtime import get_worker_runtime


async def run(arguments: argparse.Namespace) -> int:
    runtime = get_worker_runtime()
    try:
        queued = await send_news(
            runtime,
            News(
                key=arguments.key,
                title_vi=arguments.title_vi,
                body_vi=arguments.body_vi,
                title_en=arguments.title_en,
                body_en=arguments.body_en,
            ),
            operator=arguments.operator,
        )
    finally:
        await runtime.database.close()
    print(f"queued for {queued} people")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--key", required=True, help="a short slug, e.g. trip-pass-launch")
    parser.add_argument("--operator", required=True, help="who is sending (audited)")
    for language in ("vi", "en"):
        parser.add_argument(f"--title-{language}", required=True, help="1-80 characters")
        parser.add_argument(f"--body-{language}", required=True, help="1-300 characters")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
