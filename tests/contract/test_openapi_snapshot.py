from __future__ import annotations

import json
from pathlib import Path

from beluno.api.main import create_app
from beluno.config import Settings


def test_committed_openapi_snapshot_matches_application() -> None:
    snapshot_path = Path(__file__).parents[2] / "openapi" / "openapi.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))

    assert snapshot == create_app(settings=Settings()).openapi()
