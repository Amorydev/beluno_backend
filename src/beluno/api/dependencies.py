"""FastAPI dependencies shared by routers."""

from __future__ import annotations

import ipaddress
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from beluno.auth import AuthenticatedActor
from beluno.db.session import Database
from beluno.modules.context import Runtime
from beluno.sync.executor import CommandRunner

bearer_scheme = HTTPBearer(auto_error=False)
BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


def get_database(request: Request) -> Database:
    return cast(Database, request.app.state.database)


def get_runtime(request: Request) -> Runtime:
    return cast(Runtime, request.app.state.runtime)


def get_command_runner(request: Request) -> CommandRunner:
    return cast(CommandRunner, request.app.state.command_runner)


def get_current_actor(
    credentials: BearerCredentials,
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> AuthenticatedActor:
    """Verify the bearer token; current session state is re-checked in ``open_context``."""

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return runtime.tokens.verify(credentials.credentials)


def get_optional_actor(
    credentials: BearerCredentials,
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> AuthenticatedActor | None:
    """Endpoints usable both signed out and signed in (sign-in, invite redemption)."""

    if credentials is None:
        return None
    return get_current_actor(credentials, runtime)


def client_subject(request: Request) -> str:
    """Rate-limit subject for unauthenticated traffic (hashed before storage).

    IPv6 clients count per /64, the block one subscriber usually holds, so rotating
    addresses inside it does not reset the limit.
    """

    host = request.client.host if request.client else "unknown"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]
RunnerDep = Annotated[CommandRunner, Depends(get_command_runner)]
ActorDep = Annotated[AuthenticatedActor, Depends(get_current_actor)]
OptionalActorDep = Annotated[AuthenticatedActor | None, Depends(get_optional_actor)]
