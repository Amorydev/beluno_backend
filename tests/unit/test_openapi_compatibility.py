from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_openapi_compatibility.py"


def load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_openapi_compatibility", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def document(*paths: str) -> dict[str, object]:
    return {"paths": {path: {"get": {"responses": {"200": {}}}} for path in paths}}


def run(
    checker: ModuleType, tmp_path: Path, base: object, current: object, accepted: object
) -> int:
    files = {}
    for name, content in (("base", base), ("current", current), ("accepted", accepted)):
        files[name] = tmp_path / f"{name}.json"
        files[name].write_text(json.dumps(content), encoding="utf-8")
    checker.sys.argv = [  # type: ignore[attr-defined]
        "check",
        str(files["base"]),
        str(files["current"]),
        str(files["accepted"]),
    ]
    result: int = checker.main()
    return result


def test_listed_breaks_pass_and_unlisted_breaks_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checker = load_checker()
    base = document("/v1/groups", "/v1/plans")
    accepted = {
        "accepted": [
            {"break": "GET /v1/groups: operation removed", "reason": "groups", "release": "1"}
        ]
    }

    assert run(checker, tmp_path, base, document("/v1/plans"), accepted) == 0
    assert "ACCEPTED: GET /v1/groups: operation removed" in capsys.readouterr().out

    assert run(checker, tmp_path, base, document(), accepted) == 1
    assert "BREAKING: GET /v1/plans: operation removed" in capsys.readouterr().out


def test_an_accepted_break_must_say_why_and_when(tmp_path: Path) -> None:
    checker = load_checker()
    unexplained = {"accepted": [{"break": "GET /v1/groups: operation removed"}]}

    with pytest.raises(ValueError, match="reason and a release"):
        run(checker, tmp_path, document("/v1/groups"), document(), unexplained)
