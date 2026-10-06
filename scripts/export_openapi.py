"""Export the versioned public OpenAPI snapshot from the FastAPI application."""

from __future__ import annotations

import json
from pathlib import Path

from beluno.api.main import create_app
from beluno.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = PROJECT_ROOT / "openapi" / "openapi.json"


def main() -> None:
    app = create_app(settings=Settings())
    document = app.openapi()
    OUTPUT_PATH.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
