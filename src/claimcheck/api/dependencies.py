"""FastAPI dependencies for owner identity and short-lived SQLAlchemy sessions."""
from __future__ import annotations

from collections.abc import Generator

from fastapi import Request
from sqlalchemy.orm import Session

from claimcheck.application.errors import ApplicationError
from .identity import OwnerPrincipal


def get_owner_principal(request: Request) -> OwnerPrincipal:
    provider = request.app.state.identity_provider
    if provider is None:
        raise ApplicationError(
            401,
            "UNAUTHENTICATED",
            "Authentication required",
            "A supported identity provider is required.",
        )
    return provider.resolve(request)


def get_session(request: Request) -> Generator[Session, None, None]:
    engine = request.app.state.database_engine
    if engine is None:
        raise ApplicationError(
            503,
            "DATABASE_NOT_CONFIGURED",
            "Service unavailable",
            "Persistent storage is not configured.",
            retryable=True,
        )
    with Session(engine, expire_on_commit=False) as session:
        yield session
