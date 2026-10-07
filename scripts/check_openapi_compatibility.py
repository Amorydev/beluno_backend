"""Fail when the current OpenAPI document breaks clients built against a base document.

Breaking changes detected:
* a path or operation was removed;
* a property was removed from a successful JSON response schema;
* a request body or parameter became required, or a new required one appeared.

A break listed in the accepted-breaks file (``openapi/accepted-breaks.json`` by
default, or a third argument) is reported but does not fail the check. Each entry
names the exact break, why it is intended, and the release it ships in; prune the
list once the base branch carries the new contract.

Usage: python scripts/check_openapi_compatibility.py BASE.json CURRENT.json [ACCEPTED.json]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

METHODS = ("get", "put", "post", "delete", "patch")
DEFAULT_ACCEPTED = Path(__file__).resolve().parent.parent / "openapi" / "accepted-breaks.json"


def resolve(document: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = document["components"]["schemas"][name]
    return schema


def properties(document: dict[str, Any], schema: dict[str, Any] | None) -> set[str]:
    if not schema:
        return set()
    schema = resolve(document, schema)
    if schema.get("type") == "array":
        return {f"[]{name}" for name in properties(document, schema.get("items"))}
    found = set(schema.get("properties", {}))
    for member in schema.get("allOf", []):
        found |= properties(document, member)
    return found


def required_inputs(document: dict[str, Any], operation: dict[str, Any]) -> set[str]:
    required = {
        f"param:{parameter['in']}:{parameter['name']}"
        for parameter in operation.get("parameters", [])
        if parameter.get("required")
    }
    body = operation.get("requestBody")
    if body and body.get("required"):
        required.add("body")
        schema = body.get("content", {}).get("application/json", {}).get("schema")
        if schema:
            required |= {f"body:{name}" for name in resolve(document, schema).get("required", [])}
    return required


def success_properties(document: dict[str, Any], operation: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for status, response in operation.get("responses", {}).items():
        if status.startswith("2"):
            schema = response.get("content", {}).get("application/json", {}).get("schema")
            found |= {f"{status}:{name}" for name in properties(document, schema)}
    return found


def breaking_changes(base: dict[str, Any], current: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for path, base_item in base.get("paths", {}).items():
        current_item = current.get("paths", {}).get(path)
        for method in METHODS:
            if method not in base_item:
                continue
            label = f"{method.upper()} {path}"
            if current_item is None or method not in current_item:
                problems.append(f"{label}: operation removed")
                continue
            removed = success_properties(base, base_item[method]) - success_properties(
                current, current_item[method]
            )
            problems += [f"{label}: response property removed {name}" for name in sorted(removed)]
            added = required_inputs(current, current_item[method]) - required_inputs(
                base, base_item[method]
            )
            problems += [f"{label}: newly required {name}" for name in sorted(added)]
    return problems


def accepted_breaks(path: Path) -> set[str]:
    """The intended breaks; every entry must say why and in which release."""

    if not path.exists():
        return set()
    entries = json.loads(path.read_text(encoding="utf-8"))["accepted"]
    for entry in entries:
        if not entry.get("reason") or not entry.get("release"):
            raise ValueError(f"accepted break needs a reason and a release: {entry}")
    return {entry["break"] for entry in entries}


def main() -> int:
    base_path, current_path = (Path(argument) for argument in sys.argv[1:3])
    accepted = accepted_breaks(Path(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_ACCEPTED)
    problems = breaking_changes(
        json.loads(base_path.read_text(encoding="utf-8")),
        json.loads(current_path.read_text(encoding="utf-8")),
    )
    unexpected = [problem for problem in problems if problem not in accepted]
    for problem in problems:
        print(f"{'ACCEPTED' if problem in accepted else 'BREAKING'}: {problem}")
    return 1 if unexpected else 0


if __name__ == "__main__":
    raise SystemExit(main())
