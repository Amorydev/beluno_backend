"""Authenticated diagnostic routes used while domain modules are introduced."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

from beluno.api.dependencies import get_current_actor
from beluno.auth import AuthenticatedActor
from beluno.contracts.errors import PublicProblemResponse

router = APIRouter(prefix="/internal", tags=["internal"])


class CurrentActorResponse(BaseModel):
    user_id: str


@router.get(
    "/whoami",
    response_model=CurrentActorResponse,
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "model": PublicProblemResponse,
            "content": {
                "application/problem+json": {},
            },
            "description": "Bearer authentication is missing or invalid.",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": PublicProblemResponse,
            "content": {
                "application/problem+json": {},
            },
            "description": "Authentication verification is unavailable.",
        },
    },
)
async def whoami(actor: AuthenticatedActor = Depends(get_current_actor)) -> CurrentActorResponse:
    return CurrentActorResponse(user_id=str(actor.user_id))
