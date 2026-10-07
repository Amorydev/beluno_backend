"""RFC 9457-compatible error contracts."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProblemDetails(BaseModel):
    """Stable public error shape with no server internals."""

    model_config = ConfigDict(extra="forbid")

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    code: str = Field(pattern=r"^[A-Z0-9_]+$")
    request_id: str | None = None
    details: dict[str, str] | None = None
    # ``VERSION_CONFLICT`` only: the canonical current representation of the resource.
    current: dict[str, Any] | None = None


class PublicProblemResponse(BaseModel):
    """Typed-client-safe problem fields guaranteed across every API error."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    code: str
    request_id: str | None = None
    current: dict[str, Any] | None = None


class BelunoError(Exception):
    """Domain-safe error raised by application boundaries."""

    def __init__(
        self,
        *,
        status: int,
        code: str,
        title: str,
        detail: str | None = None,
        details: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail or title)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.details = details
        self.headers = headers
        self.current: dict[str, Any] | None = None


class VersionConflictError(BelunoError):
    """A stale expected version; carries the entity so the executor can attach ``current``."""

    def __init__(self, entity: object | None = None) -> None:
        super().__init__(
            status=412,
            code="VERSION_CONFLICT",
            title="Resource version has changed",
            detail="Reload the resource and retry with its current version",
        )
        self.entity = entity


def not_found() -> BelunoError:
    """Absent or intentionally undisclosed; callers never reveal which."""

    return BelunoError(status=404, code="NOT_FOUND", title="Resource not found")


def forbidden(detail: str | None = None) -> BelunoError:
    return BelunoError(status=403, code="FORBIDDEN", title="Action is not allowed", detail=detail)


def step_up_required() -> BelunoError:
    return BelunoError(
        status=403,
        code="STEP_UP_REQUIRED",
        title="Recent sign-in required",
        detail="Sign in again to confirm this sensitive action",
    )


def conflict(code: str, title: str, detail: str | None = None) -> BelunoError:
    return BelunoError(status=409, code=code, title=title, detail=detail)


def invalid_state(detail: str) -> BelunoError:
    return conflict("INVALID_STATE_TRANSITION", "Action is not valid in the current state", detail)


def version_conflict(entity: object | None = None) -> BelunoError:
    """``entity`` is the row whose version was stale; its snapshot is returned as ``current``."""

    return VersionConflictError(entity)


def precondition_required() -> BelunoError:
    return BelunoError(
        status=428,
        code="PRECONDITION_REQUIRED",
        title="If-Match header is required",
    )


def validation_error(detail: str) -> BelunoError:
    return BelunoError(
        status=422, code="VALIDATION_FAILED", title="Request validation failed", detail=detail
    )


def rate_limited(retry_after_seconds: int) -> BelunoError:
    return BelunoError(
        status=429,
        code="RATE_LIMITED",
        title="Too many requests",
        headers={"Retry-After": str(max(1, retry_after_seconds))},
    )


def upgrade_required(detail: str) -> BelunoError:
    """A paid feature on a trip that neither a Trip Pass nor its owner's Pro unlocks."""

    return BelunoError(status=403, code="UPGRADE_REQUIRED", title="Upgrade required", detail=detail)


def feature_disabled() -> BelunoError:
    return BelunoError(
        status=503,
        code="FEATURE_DISABLED",
        title="This feature is temporarily unavailable",
    )


def authentication_failed() -> BelunoError:
    return BelunoError(
        status=401,
        code="AUTHENTICATION_REQUIRED",
        title="Authentication required",
        detail="Invalid or expired credentials",
    )


def authentication_unavailable(detail: str = "Authentication is not configured") -> BelunoError:
    return BelunoError(
        status=503,
        code="AUTHENTICATION_UNAVAILABLE",
        title="Authentication is temporarily unavailable",
        detail=detail,
    )


def invite_unavailable() -> BelunoError:
    """One response for unknown, expired, revoked, and exhausted invites."""

    return BelunoError(
        status=404,
        code="INVITE_UNAVAILABLE",
        title="Invite is not available",
        detail="This invite link is invalid or no longer active",
    )
