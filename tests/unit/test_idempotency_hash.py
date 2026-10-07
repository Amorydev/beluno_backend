from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from beluno.sync.commands import Command, CommandCall
from beluno.sync.idempotency import request_hash
from beluno.token_hashing import TokenHasher


class Secretive(BaseModel):
    confirmation_code: str


COMMAND: Command[Any, Any] = Command(
    name="booking.create",
    payload_model=Secretive,
    response_model=None,
    handler=None,  # type: ignore[arg-type]
)


def context(key: str) -> Any:
    return SimpleNamespace(runtime=SimpleNamespace(require_hasher=lambda: TokenHasher(key)))


def test_stored_request_digests_cannot_be_recomputed_without_the_key() -> None:
    call = CommandCall(target={"plan_id": uuid4()})
    payload = Secretive(confirmation_code="Q7XK")
    stored = request_hash(context("a" * 40), COMMAND, call, payload)
    assert stored == request_hash(context("a" * 40), COMMAND, call, payload)
    assert stored != request_hash(context("b" * 40), COMMAND, call, payload)
    canonical = {
        "command": COMMAND.name,
        "schema_version": COMMAND.schema_version,
        "target": {name: str(value) for name, value in call.target.items()},
        "expected_version": None,
        "payload": {"confirmation_code": "Q7XK"},
    }
    plain = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    assert stored != hashlib.sha256(plain).digest()
