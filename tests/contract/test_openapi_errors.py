from __future__ import annotations

from beluno.api.main import create_app
from beluno.config import Settings


def test_openapi_documents_runtime_error_responses() -> None:
    paths = create_app(settings=Settings()).openapi()["paths"]
    readiness = paths["/health/ready"]["get"]["responses"]
    whoami = paths["/internal/whoami"]["get"]["responses"]

    assert "503" in readiness
    assert "401" in whoami
    assert "503" in whoami
    assert "application/problem+json" in whoami["401"]["content"]
