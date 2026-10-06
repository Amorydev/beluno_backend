"""Apply platform migrations, queue schema, and least-privilege grants."""

from __future__ import annotations

import asyncio

from beluno.config import get_settings
from beluno.db.bootstrap import bootstrap_job_schema, run_migrations


def main() -> None:
    settings = get_settings()
    run_migrations(settings)
    asyncio.run(bootstrap_job_schema(settings))


if __name__ == "__main__":
    main()
