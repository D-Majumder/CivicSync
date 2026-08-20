"""
Database engine and session infrastructure for CivicSync.

Configuration-driven: the connection string comes from the DATABASE_URL
environment variable (falling back to a local SQLite file for
development). Nothing else in the codebase should hardcode a database
path -- to move to PostgreSQL later, only DATABASE_URL needs to change.
"""

from __future__ import annotations

import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# Read at import time so the value is resolved once per process. In the
# running app, backend/main.py calls load_dotenv() before importing
# anything that needs env vars; standalone tools (Alembic, tests) load
# their own env as needed -- see alembic/env.py and tests/conftest.py.
DATABASE_URL: str = os.environ.get("DATABASE_URL", "sqlite:///./civicsync.db")

# check_same_thread=False is only meaningful for SQLite: FastAPI may hand a
# request's session off across async machinery on the same worker thread,
# and SQLite's default driver otherwise refuses cross-"thread" use of a
# single connection. It's a no-op for other backends (e.g. PostgreSQL).
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models (see backend/models.py)."""


def build_engine(database_url: str = DATABASE_URL) -> Engine:
    """Create a new Engine for the given database URL.

    Factored out (rather than only exposing a single module-level engine)
    so tests and tooling can point at an isolated database without
    touching the default civicsync.db file.
    """
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args)


# Default engine/session factory used by the running application.
# Tables are NOT created here -- schema creation is Alembic's job
# (see alembic/ and "Database Initialization" in the project README).
engine: Engine = build_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a request-scoped session and closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
