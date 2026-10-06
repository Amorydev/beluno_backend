"""OpenAPI documentation for ``application/problem+json`` error responses."""

from __future__ import annotations

from typing import Any

from beluno.contracts.errors import PublicProblemResponse

PROBLEM_DESCRIPTIONS = {
    401: "Bearer authentication is missing, invalid, expired, or revoked.",
    403: "The current relationship does not allow this action.",
    404: "The resource is absent or not visible to the caller.",
    409: "The request conflicts with the current resource state.",
    412: "The If-Match version is stale.",
    422: "The request does not satisfy the contract.",
    426: "The client protocol or schema version is not supported; upgrade the client.",
    428: "An If-Match header is required.",
    429: "Too many requests; retry after the Retry-After interval.",
    503: "A dependency or feature is temporarily unavailable.",
}


def problem_responses(*status_codes: int) -> dict[int | str, dict[str, Any]]:
    return {
        code: {
            "model": PublicProblemResponse,
            "content": {"application/problem+json": {}},
            "description": PROBLEM_DESCRIPTIONS[code],
        }
        for code in status_codes
    }


AUTHENTICATED = (401, 503)
