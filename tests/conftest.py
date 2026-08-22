"""
Shared pytest fixtures for CivicSync's test suite.

`db_session` provides each database test with its own throwaway SQLite
file, created fresh from backend.database.Base.metadata (i.e. from the
ORM models directly, not via Alembic) and destroyed afterward. It never
touches the application's real civicsync.db.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base, build_engine

# Import backend.models so Issue registers its table on Base.metadata
# before create_all() runs below.
import backend.models  # noqa: F401


@pytest.fixture(autouse=True)
def _default_jurisdiction_env(monkeypatch):
    """Milestone 17: every new Issue requires a resolvable default
    jurisdiction (CIVICSYNC_DEFAULT_JURISDICTION_CODE). Set globally here
    so individual test files don't each need to configure it -- any test
    exercising the "missing/invalid config" failure path can still
    override or unset it via its own monkeypatch call, which takes
    precedence within that test and reverts automatically afterward.
    """
    monkeypatch.setenv("CIVICSYNC_DEFAULT_JURISDICTION_CODE", "IN-WB-NADIA-KRISHNANAGAR")


@pytest.fixture
def db_session(tmp_path) -> Generator[Session, None, None]:
    """Yield a Session backed by a fresh, temporary SQLite database file."""
    db_path = tmp_path / "test_civicsync.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)

    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
