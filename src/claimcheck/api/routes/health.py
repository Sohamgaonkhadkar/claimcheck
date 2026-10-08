"""Liveness/readiness endpoints; legacy Phase 1 probes remain compatible."""
from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from claimcheck.application.errors import ApplicationError
from claimcheck.api.schemas import HealthResponse, ReadinessResponse, ReadyResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Phase 1 liveness check")
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/ready", response_model=ReadyResponse,
            summary="Phase 1 compatibility readiness probe")
def ready() -> ReadyResponse:
    # Kept as the original Phase 1 smoke endpoint and response. Deployments use /readyz below.
    return ReadyResponse(status="ready")


@router.get("/healthz", summary="Liveness check")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": "0.3.0a1"}


@router.get("/readyz", response_model=ReadinessResponse,
            summary="Check PostgreSQL and private storage readiness")
def readyz(request: Request) -> ReadinessResponse:
    engine = request.app.state.database_engine
    if engine is None:
        raise ApplicationError(
            503, "DATABASE_NOT_CONFIGURED", "Service unavailable",
            "Persistent storage is not configured.", retryable=True,
        )
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        storage = request.app.state.storage
        check_ready = getattr(storage, "check_ready", None)
        if not callable(check_ready) or check_ready() is not True:
            raise OSError("Storage readiness check failed.")
    except ApplicationError:
        raise
    except (SQLAlchemyError, OSError) as exc:
        raise ApplicationError(
            503, "DEPENDENCY_UNAVAILABLE", "Service unavailable",
            "A required persistence service is not ready.", retryable=True,
        ) from exc
    return ReadinessResponse(status="ready")
