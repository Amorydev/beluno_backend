"""The sync fixture corpus stays valid against the published contracts.

Clients build their encoders/decoders against these documents; a contract
change that invalidates one of them is a compatibility break to review.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from beluno.contracts.sync import (
    HandshakeRequest,
    HandshakeResponse,
    PullRequest,
    PullResponse,
    PushRequest,
    PushResponse,
)

FIXTURES = Path(__file__).parent / "sync-fixtures"
CONTRACTS: dict[str, tuple[type[BaseModel], type[BaseModel]]] = {
    "handshake": (HandshakeRequest, HandshakeResponse),
    "push": (PushRequest, PushResponse),
    "pull": (PullRequest, PullResponse),
}


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_fixture_matches_contract(name: str) -> None:
    document = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    request_model, response_model = CONTRACTS[name]
    request = request_model.model_validate(document["request"])
    response = response_model.model_validate(document["response"])
    assert request.model_dump(mode="json", exclude_unset=True).keys() <= document["request"].keys()
    assert response.model_dump(mode="json") == response_model.model_validate(
        response.model_dump(mode="json")
    ).model_dump(mode="json")


def test_push_fixture_marks_dependency_skips() -> None:
    document = json.loads((FIXTURES / "push.json").read_text(encoding="utf-8"))
    results = PushResponse.model_validate(document["response"]).results
    assert [result.outcome for result in results] == ["conflict", "skipped"]
    assert results[0].problem is not None and results[0].problem.current is not None
