"""PostgreSQL engine/session construction. Product state never falls back to SQLite."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session


def normalize_postgres_url(database_url: str) -> str:
    """Require PostgreSQL and use the declared psycopg 3 driver, never SQLite."""
    url = make_url(database_url)
    if url.drivername not in {"postgresql", "postgresql+psycopg"}:
        raise ValueError("CLAIMCHECK product-state persistence requires PostgreSQL with psycopg.")
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    return url.render_as_string(hide_password=False)


def create_postgres_engine(database_url: str) -> Engine:
    """Create a PostgreSQL engine, normalizing the bare URL to psycopg 3."""
    return create_engine(
        normalize_postgres_url(database_url), pool_pre_ping=True, pool_size=5, max_overflow=10
    )


def session_scope(engine: Engine) -> Iterator[Session]:
    """Yield a short-lived session; application services own transaction boundaries."""
    with Session(engine, expire_on_commit=False) as session:
        yield session
