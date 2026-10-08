"""FastAPI entry point for the preserved Phase 1 slice and Phase 2 backend resources."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from claimcheck.application.errors import ApplicationError
from claimcheck.application.golden_demo import GoldenDemoService
from claimcheck.persistence.database import create_postgres_engine
from claimcheck.persistence.storage import PrivateLocalFileStorage

from .config import AppSettings
from .errors import (
    application_exception_handler,
    database_exception_handler,
    http_exception_handler,
    unexpected_exception_handler,
    validation_exception_handler,
)
from .identity import DevelopmentIdentityProvider, OwnerIdentityProvider
from .routes import cases, dev, health


def create_app(
    settings: AppSettings | None = None,
    golden_demo_service: GoldenDemoService | None = None,
    *,
    database_engine: Engine | None = None,
    storage=None,
    identity_provider: OwnerIdentityProvider | None = None,
) -> FastAPI:
    """Create the HTTP adapter with injectable infrastructure and identity ports.

    The development identity is a fixed, server-configured local principal. No request header or
    body field selects an owner. Production deployments without an injected OIDC provider receive
    401 for case resources; this repository does not pretend that the local identity is auth.
    """
    settings = settings or AppSettings.from_environment()
    development = settings.development_mode
    if database_engine is None and settings.database_url:
        database_engine = create_postgres_engine(settings.database_url)
    if storage is None:
        root = settings.storage_root or str(
            Path.home() / ".local" / "share" / "claimcheck" / "private-storage"
        )
        storage = PrivateLocalFileStorage(root)
    if identity_provider is None and development:
        identity_provider = DevelopmentIdentityProvider(settings.development_owner_id)
    if (identity_provider is not None
            and getattr(identity_provider, "auth_mode", None) == "development"
            and not development):
        raise ValueError("Development identity cannot be enabled outside development mode.")

    app = FastAPI(
        title="CLAIMCHECK API",
        version="0.3.0a1",
        description="Evidence-first claim workflow API; Phase 2 persists product state only.",
        docs_url="/docs" if development else None,
        redoc_url=None,
        openapi_url="/openapi.json" if development else None,
    )
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
            expose_headers=["Location", "X-Request-ID"],
            max_age=600,
        )
    app.state.settings = settings
    app.state.golden_demo_service = golden_demo_service or GoldenDemoService()
    app.state.database_engine = database_engine
    app.state.storage = storage
    app.state.identity_provider = identity_provider

    app.include_router(health.router)
    app.include_router(cases.router)
    if development:
        app.include_router(dev.router)

    # Safe handlers also cover framework-generated 404/405 and validation responses.
    app.add_exception_handler(ApplicationError, application_exception_handler)
    app.add_exception_handler(404, http_exception_handler)
    app.add_exception_handler(405, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(SQLAlchemyError, database_exception_handler)
    app.add_exception_handler(Exception, unexpected_exception_handler)

    @app.middleware("http")
    async def request_id_header(request: Request, call_next):
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    return app


# Production is the safe default. Configure PostgreSQL and OIDC before serving real users.
app = create_app()
