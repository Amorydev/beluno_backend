"""Liveness and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from beluno.api.dependencies import get_database
from beluno.db.session import Database

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str


@router.get("/health/live", response_model=HealthResponse)
async def liveness() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get(
    "/health/ready",
    response_model=HealthResponse,
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": HealthResponse,
            "description": "A required dependency is unavailable.",
        },
    },
)
async def readiness(database: Database = Depends(get_database)) -> HealthResponse | JSONResponse:
    if await database.check():
        return HealthResponse(status="ok")
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "degraded"},
    )
