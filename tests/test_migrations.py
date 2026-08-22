"""
Tests for the backfill_missing_initial_history data migration
(alembic/versions/11c4670a4773_backfill_missing_initial_status_history.py).

This migration repairs pre-existing Issues that are missing their initial
null -> SUBMITTED status-history row (see that migration's docstring for
the root-cause explanation). These tests load the migration module
directly by file path and call its core function against a temporary
SQLite database -- no real civicsync.db is touched, and no Alembic
command-line invocation is needed.
"""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ai.schemas import IssueCategory, SeverityLevel
from backend.database import Base
from backend.models import Issue, IssueStatus, Jurisdiction, JurisdictionLevel
from backend.repository import (
    create_issue_from_civic_issue,
    get_status_history,
    transition_issue_status,
)
from ai.schemas import CivicIssue

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "11c4670a4773_backfill_missing_initial_status_history.py"
)


@pytest.fixture
def migration_module():
    spec = importlib.util.spec_from_file_location("backfill_migration", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db_session_and_engine(tmp_path):
    db_path = tmp_path / "test_migration.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestingSessionLocal()
    session.add(
        Jurisdiction(
            code="IN-WB-NADIA-KRISHNANAGAR", name="Krishnanagar Municipality",
            level=JurisdictionLevel.LOCAL_BODY, country_code="IN",
        )
    )
    session.commit()
    jurisdiction_id = session.query(Jurisdiction).filter(
        Jurisdiction.code == "IN-WB-NADIA-KRISHNANAGAR"
    ).one().id
    try:
        yield session, engine, jurisdiction_id
    finally:
        session.close()
        engine.dispose()


def _make_issue_missing_initial_history(
    db_session, created_at: datetime, jurisdiction_id: int
) -> Issue:
    """Simulate a pre-existing Issue created by an older code path that
    never wrote the initial history row -- i.e. the exact bug reported."""
    issue = Issue(
        original_text="No street light near our school for two weeks.",
        category=IssueCategory.STREET_LIGHTING,
        problem="Non-functional street light near a school.",
        severity=SeverityLevel.MEDIUM,
        confidence=0.8,
        status=IssueStatus.SUBMITTED,
        created_at=created_at,
        jurisdiction_id=jurisdiction_id,
    )
    db_session.add(issue)
    db_session.commit()
    db_session.refresh(issue)
    return issue


def test_migration_backfills_missing_initial_row(migration_module, db_session_and_engine):
    """Reproduces the reported scenario exactly: an issue with 6 recorded
    transitions (SUBMITTED->CLASSIFIED ... RESOLVED->CLOSED) but no initial
    null->SUBMITTED row, then confirms the migration adds exactly that row."""
    db_session, engine, jurisdiction_id = db_session_and_engine
    old_created_at = datetime.now(timezone.utc) - timedelta(days=30)
    issue = _make_issue_missing_initial_history(db_session, old_created_at, jurisdiction_id)

    for target in (
        IssueStatus.CLASSIFIED,
        IssueStatus.ROUTED,
        IssueStatus.ACKNOWLEDGED,
        IssueStatus.IN_PROGRESS,
        IssueStatus.RESOLVED,
        IssueStatus.CLOSED,
    ):
        transition_issue_status(db_session, issue, target)

    history_before = get_status_history(db_session, issue)
    assert len(history_before) == 6
    assert all(entry.from_status is not None for entry in history_before)

    with engine.connect() as conn:
        inserted = migration_module.backfill_missing_initial_history(conn)
        conn.commit()
    assert inserted == 1

    db_session.expire_all()
    history_after = get_status_history(db_session, issue)
    assert len(history_after) == 7
    assert history_after[0].from_status is None
    assert history_after[0].to_status == IssueStatus.SUBMITTED
    assert history_after[0].reason == "Issue submitted."
    # Historical accuracy: backfilled changed_at matches the issue's own
    # created_at, not "now" (when the migration happened to run).
    assert history_after[0].changed_at.replace(
        tzinfo=timezone.utc
    ) == old_created_at.replace(microsecond=history_after[0].changed_at.microsecond)


def test_migration_is_idempotent(migration_module, db_session_and_engine):
    db_session, engine, jurisdiction_id = db_session_and_engine
    issue = _make_issue_missing_initial_history(db_session, datetime.now(timezone.utc), jurisdiction_id)

    with engine.connect() as conn:
        first_run = migration_module.backfill_missing_initial_history(conn)
        conn.commit()
    assert first_run == 1

    with engine.connect() as conn:
        second_run = migration_module.backfill_missing_initial_history(conn)
        conn.commit()
    assert second_run == 0

    db_session.expire_all()
    history = get_status_history(db_session, issue)
    assert len(history) == 1


def test_migration_does_not_touch_issues_that_already_have_initial_row(
    migration_module, db_session_and_engine
):
    """Issues created through the current (correct) code path already have
    their initial row -- the migration must not add a duplicate."""
    db_session, engine, jurisdiction_id = db_session_and_engine
    civic_issue = CivicIssue(
        original_text="Garbage has not been collected in ten days.",
        category=IssueCategory.SANITATION_AND_WASTE,
        problem="Garbage collection has not occurred.",
        severity=SeverityLevel.LOW,
        confidence=0.7,
    )
    issue = create_issue_from_civic_issue(db_session, civic_issue, jurisdiction_id)

    with engine.connect() as conn:
        inserted = migration_module.backfill_missing_initial_history(conn)
        conn.commit()
    assert inserted == 0

    db_session.expire_all()
    history = get_status_history(db_session, issue)
    assert len(history) == 1
    assert history[0].from_status is None
    assert history[0].to_status == IssueStatus.SUBMITTED


def test_migration_handles_multiple_issues_independently(
    migration_module, db_session_and_engine
):
    db_session, engine, jurisdiction_id = db_session_and_engine
    broken_issue = _make_issue_missing_initial_history(db_session, datetime.now(timezone.utc), jurisdiction_id)
    civic_issue = CivicIssue(
        original_text="Pothole on Main Street.",
        category=IssueCategory.ROADS_AND_POTHOLES,
        problem="Large pothole causing traffic hazard.",
        severity=SeverityLevel.HIGH,
        confidence=0.9,
    )
    healthy_issue = create_issue_from_civic_issue(db_session, civic_issue, jurisdiction_id)

    with engine.connect() as conn:
        inserted = migration_module.backfill_missing_initial_history(conn)
        conn.commit()
    assert inserted == 1

    db_session.expire_all()
    assert len(get_status_history(db_session, broken_issue)) == 1
    assert len(get_status_history(db_session, healthy_issue)) == 1
