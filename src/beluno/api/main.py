"""Beluno FastAPI application factory."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from beluno import __version__
from beluno.api.commands import build_registry
from beluno.api.routers import (
    auth,
    billing,
    crews,
    exports,
    finance,
    health,
    internal,
    invites,
    me,
    media,
    notifications,
    passkeys,
    planning,
    plans,
    recap,
    support,
    sync,
)
from beluno.auth import AccessTokenCodec
from beluno.config import Settings, get_settings
from beluno.contracts.errors import BelunoError, ProblemDetails
from beluno.db.session import Database
from beluno.modules.context import Runtime, utc_now
from beluno.modules.iam.external_identity import ExternalIdentityVerifier
from beluno.observability.context import get_request_id, reset_request_id, set_request_id
from beluno.observability.setup import configure_observability, logger, safe_extra
from beluno.stores import GooglePlay
from beluno.sync.executor import CommandRunner
from beluno.token_hashing import TokenHasher

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


error_log = logger("beluno.api")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Client correlation IDs are echoed only when safe to log and store.
        supplied = request.headers.get("X-Request-ID", "")
        request_id = supplied if REQUEST_ID_PATTERN.match(supplied) else str(uuid4())
        context_token = set_request_id(request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            reset_request_id(context_token)


class RequestBodyLimitMiddleware:
    """Reject over-limit bodies before routing, including chunked requests."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope["headers"]}
        content_length = headers.get(b"content-length")
        if content_length is not None and int(content_length) > self.max_bytes:
            await self._send_too_large(scope, receive, send)
            return

        messages: list[Message] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    await self._send_too_large(scope, receive, send)
                    return
            messages.append(message)
            if not message.get("more_body", False):
                break

        async def replay_receive() -> Message:
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)

    async def _send_too_large(self, scope: Scope, receive: Receive, send: Send) -> None:
        problem = ProblemDetails(
            title="Request body too large",
            status=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Request body exceeds the configured size limit",
            instance=str(scope["path"]),
            code="REQUEST_TOO_LARGE",
            request_id=get_request_id(),
        )
        response = JSONResponse(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            content=problem.model_dump(exclude_none=True),
            media_type="application/problem+json",
        )
        await response(scope, receive, send)


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
    *,
    identity_verifier: ExternalIdentityVerifier | None = None,
    clock: Callable[[], datetime] | None = None,
    google_play: GooglePlay | None = None,
) -> FastAPI:
    active_settings = settings or get_settings()
    active_database = database or Database(active_settings)
    runtime = Runtime(
        settings=active_settings,
        database=active_database,
        tokens=AccessTokenCodec(active_settings),
        hasher=TokenHasher.from_settings(active_settings),
        identity_verifier=identity_verifier or ExternalIdentityVerifier(active_settings),
        clock=clock or utc_now,
        google_play_client=google_play,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await active_database.close()

    app = FastAPI(
        title="Beluno API",
        version=__version__,
        description="API for plans with your people.",
        lifespan=lifespan,
    )
    app.state.database = active_database
    app.state.runtime = runtime
    app.state.command_runner = CommandRunner(runtime, build_registry())
    configure_observability(app, active_settings)
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=active_settings.api_max_request_bytes)
    app.add_middleware(RequestContextMiddleware)
    for router in (
        health.router,
        internal.router,
        auth.router,
        me.router,
        passkeys.router,
        notifications.router,
        crews.router,
        plans.router,
        finance.currency_router,
        finance.fx_router,
        finance.router,
        planning.router,
        media.router,
        exports.router,
        recap.router,
        support.router,
        billing.router,
        invites.router,
        sync.router,
    ):
        app.include_router(router)

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        document = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        # Error responses are documented once as application/problem+json only.
        for path_item in document["paths"].values():
            for operation in path_item.values():
                for response in operation.get("responses", {}).values():
                    content = response.get("content", {})
                    problem_content = content.get("application/problem+json")
                    json_content = content.get("application/json")
                    if problem_content is not None and json_content is not None:
                        problem_content["schema"] = json_content["schema"]
                        del content["application/json"]
        app.openapi_schema = document
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]

    def problem_response(
        request: Request,
        *,
        status_code: int,
        code: str,
        title: str,
        detail: str | None = None,
        details: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        current: dict[str, Any] | None = None,
    ) -> JSONResponse:
        problem = ProblemDetails(
            title=title,
            status=status_code,
            detail=detail,
            instance=str(request.url.path),
            code=code,
            request_id=get_request_id(),
            details=details,
            current=current,
        )
        return JSONResponse(
            status_code=status_code,
            content=problem.model_dump(exclude_none=True),
            media_type="application/problem+json",
            headers=headers,
        )

    @app.exception_handler(BelunoError)
    async def handle_beluno_error(request: Request, error: BelunoError) -> JSONResponse:
        return problem_response(
            request,
            status_code=error.status,
            code=error.code,
            title=error.title,
            detail=error.detail,
            details=error.details,
            headers=error.headers,
            current=error.current,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        request: Request,
        error: StarletteHTTPException,
    ) -> JSONResponse:
        error_details = {
            status.HTTP_401_UNAUTHORIZED: ("AUTHENTICATION_REQUIRED", "Authentication required"),
            status.HTTP_403_FORBIDDEN: ("FORBIDDEN", "Action is not allowed"),
            status.HTTP_404_NOT_FOUND: ("NOT_FOUND", "Resource not found"),
            status.HTTP_503_SERVICE_UNAVAILABLE: (
                "AUTHENTICATION_UNAVAILABLE",
                "Authentication is temporarily unavailable",
            ),
        }
        code, title = error_details.get(error.status_code, ("HTTP_ERROR", "Request failed"))
        detail = error.detail if isinstance(error.detail, str) else None
        return problem_response(
            request,
            status_code=error.status_code,
            code=code,
            title=title,
            detail=detail,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, error: Exception) -> JSONResponse:
        # Our log line names only the error type (database errors can carry row values).
        # Starlette still re-raises afterwards so the server and Sentry see the failure.
        error_log.error(
            "unhandled error", **safe_extra(event="unhandled_error", error=type(error).__name__)
        )
        return problem_response(
            request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="INTERNAL_ERROR",
            title="Internal server error",
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        # Field locations and messages only; submitted values are never echoed.
        details = {
            ".".join(str(part) for part in item.get("loc", ())): str(item.get("msg", "invalid"))
            for item in error.errors()[:20]
        }
        return problem_response(
            request,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="VALIDATION_FAILED",
            title="Request validation failed",
            details=details or None,
        )

    return app


app = create_app()
