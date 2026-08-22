"""
Tests for the database persistence layer (backend/database.py,
backend/models.py, backend/repository.py).

Every test uses the `db_session` fixture (tests/conftest.py), which
creates a throwaway SQLite file per test and tears it down afterward --
none of these tests touch the application's real civicsync.db, and none
of them call Gemini.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from ai.schemas import CivicIssue, IssueCategory, SeverityLevel
from backend.models import Department, Issue, IssueStatus, Jurisdiction, JurisdictionLevel
from backend.repository import create_issue_from_civic_issue, get_issue_by_public_id


def _sample_civic_issue(**overrides) -> CivicIssue:
    base = dict(
        original_text="There has been no street light near our school for two weeks.",
        category=IssueCategory.STREET_LIGHTING,
        problem="Non-functional street light near a school.",
        location="Near the school",
        duration="Two weeks",
        severity=SeverityLevel.MEDIUM,
        affected_population="Students and pedestrians",
        suggested_department="Municipal Electrical Department",
        confidence=0.82,
    )
    base.update(overrides)
    return CivicIssue(**base)


def _seed_jurisdiction_id(db_session) -> int:
    """Milestone 17: Issue.jurisdiction_id is required. Seeds one
    jurisdiction and returns its id -- call ONCE per test (the code is
    unique), then reuse the returned id for every Issue in that test."""
    jurisdiction = Jurisdiction(
        code="IN-WB-NADIA-KRISHNANAGAR", name="Krishnanagar Municipality",
        level=JurisdictionLevel.LOCAL_BODY, country_code="IN",
    )
    db_session.add(jurisdiction)
    db_session.commit()
    return jurisdiction.id


# --- 1. Database session can connect ---------------------------------------


def test_session_can_connect(db_session):
    result = db_session.execute(text("SELECT 1")).scalar_one()
    assert result == 1


def test_issues_table_exists_with_expected_columns(db_session):
    inspector = inspect(db_session.get_bind())
    assert "issues" in inspector.get_table_names()
    columns = {col["name"] for col in inspector.get_columns("issues")}
    assert columns == {
        "id",
        "public_id",
        "original_text",
        "category",
        "problem",
        "location",
        "duration",
        "severity",
        "affected_population",
        "suggested_department",
        "confidence",
        "jurisdiction_id",
        "assigned_department_id",
        "status",
        "resolution_summary",
        "created_at",
        "updated_at",
        "resolved_at",
        "closed_at",
    }


# --- 2 & 3. Issue can be created, persisted, and retrieved -----------------


def test_create_and_retrieve_issue(db_session):
    jid = _seed_jurisdiction_id(db_session)
    civic_issue = _sample_civic_issue()

    created = create_issue_from_civic_issue(db_session, civic_issue, jid)
    assert created.id is not None

    fetched = get_issue_by_public_id(db_session, created.public_id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.original_text == civic_issue.original_text
    assert fetched.category == IssueCategory.STREET_LIGHTING
    assert fetched.problem == civic_issue.problem
    assert fetched.confidence == pytest.approx(0.82)
    assert fetched.jurisdiction_id == jid


def test_get_issue_by_public_id_returns_none_when_missing(db_session):
    assert get_issue_by_public_id(db_session, "CIV-DOES-NOT-EXIST") is None


# --- 4. public_id is unique -------------------------------------------------


def test_public_id_is_unique(db_session):
    jid = _seed_jurisdiction_id(db_session)
    issue_a = create_issue_from_civic_issue(db_session, _sample_civic_issue(), jid)

    # Force a collision by reusing the same public_id on a second row.
    duplicate = Issue(
        public_id=issue_a.public_id,
        original_text="Different complaint text.",
        category=IssueCategory.OTHER,
        problem="Different problem.",
        severity=SeverityLevel.UNKNOWN,
        confidence=0.4,
        jurisdiction_id=jid,
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_public_ids_are_distinct_across_created_issues(db_session):
    jid = _seed_jurisdiction_id(db_session)
    first = create_issue_from_civic_issue(db_session, _sample_civic_issue(), jid)
    second = create_issue_from_civic_issue(
        db_session, _sample_civic_issue(original_text="A different complaint entirely."), jid
    )
    assert first.public_id != second.public_id


# --- 5. status values are controlled ---------------------------------------


def test_status_defaults_to_submitted(db_session):
    jid = _seed_jurisdiction_id(db_session)
    issue = create_issue_from_civic_issue(db_session, _sample_civic_issue(), jid)
    assert issue.status == IssueStatus.SUBMITTED


def test_invalid_status_value_is_rejected_by_the_database(db_session):
    jid = _seed_jurisdiction_id(db_session)
    issue = Issue(
        original_text="Complaint text.",
        category=IssueCategory.OTHER,
        problem="Some problem.",
        severity=SeverityLevel.UNKNOWN,
        confidence=0.5,
        jurisdiction_id=jid,
    )
    db_session.add(issue)
    db_session.commit()

    # Bypass the Python enum and force an invalid raw value straight into
    # the database to prove the CHECK constraint (not just Python-side
    # typing) is what enforces valid status values.
    with pytest.raises(IntegrityError):
        db_session.execute(
            text("UPDATE issues SET status = :bad WHERE id = :id"),
            {"bad": "NOT_A_REAL_STATUS", "id": issue.id},
        )
        db_session.commit()


# --- 6. timestamps are populated appropriately ------------------------------


def test_created_at_and_updated_at_are_populated_on_creation(db_session):
    jid = _seed_jurisdiction_id(db_session)
    before = datetime.now(timezone.utc)
    issue = create_issue_from_civic_issue(db_session, _sample_civic_issue(), jid)
    after = datetime.now(timezone.utc)

    assert issue.created_at is not None
    assert issue.updated_at is not None
    assert before <= issue.created_at.replace(tzinfo=timezone.utc) <= after
    assert before <= issue.updated_at.replace(tzinfo=timezone.utc) <= after


def test_updated_at_changes_on_update(db_session):
    jid = _seed_jurisdiction_id(db_session)
    issue = create_issue_from_civic_issue(db_session, _sample_civic_issue(), jid)
    original_updated_at = issue.updated_at

    issue.resolution_summary = "Repair crew dispatched."
    db_session.commit()
    db_session.refresh(issue)

    assert issue.updated_at >= original_updated_at


# --- 7. resolved_at / closed_at stay nullable initially --------------------


def test_resolved_at_and_closed_at_are_null_on_creation(db_session):
    jid = _seed_jurisdiction_id(db_session)
    issue = create_issue_from_civic_issue(db_session, _sample_civic_issue(), jid)
    assert issue.resolved_at is None
    assert issue.closed_at is None
    assert issue.status != IssueStatus.RESOLVED
    assert issue.status != IssueStatus.CLOSED


def test_resolved_at_is_only_set_by_an_explicit_operational_action(db_session):
    """AI output alone must never move an issue to RESOLVED/CLOSED or set
    resolved_at/closed_at -- that's an explicit operational action."""
    jid = _seed_jurisdiction_id(db_session)
    issue = create_issue_from_civic_issue(db_session, _sample_civic_issue(confidence=0.99), jid)
    assert issue.resolved_at is None
    assert issue.status == IssueStatus.SUBMITTED

    # Simulate the explicit operational action a future CRUD API would perform.
    issue.status = IssueStatus.RESOLVED
    issue.resolved_at = datetime.now(timezone.utc)
    db_session.commit()
    db_session.refresh(issue)

    assert issue.status == IssueStatus.RESOLVED
    assert issue.resolved_at is not None
    assert issue.closed_at is None


# --- 8. AI suggestion vs. official assignment are separate fields ----------


def test_suggested_and_assigned_department_are_independent(db_session):
    """suggested_department (AI free text) and assigned_department (an
    official Department entity, added in Milestone 6) are independent:
    assigning a department never alters the AI's original suggestion."""
    jid = _seed_jurisdiction_id(db_session)
    civic_issue = _sample_civic_issue(suggested_department="Municipal Electrical Department")
    issue = create_issue_from_civic_issue(db_session, civic_issue, jid)

    assert issue.suggested_department == "Municipal Electrical Department"
    assert issue.assigned_department is None

    department = Department(code="ELECTRICITY", name="Electricity", is_active=True)
    db_session.add(department)
    db_session.commit()

    issue.assigned_department_id = department.id
    db_session.commit()
    db_session.refresh(issue)

    assert issue.suggested_department == "Municipal Electrical Department"
    assert issue.assigned_department is not None
    assert issue.assigned_department.code == "ELECTRICITY"


def test_ai_confidence_does_not_affect_official_status(db_session):
    """High AI confidence must not itself change operational status."""
    jid = _seed_jurisdiction_id(db_session)
    issue = create_issue_from_civic_issue(db_session, _sample_civic_issue(confidence=1.0), jid)
    assert issue.status == IssueStatus.SUBMITTED


# --- 9. jurisdiction_id is required and independent of assigned_department -


def test_jurisdiction_id_is_required(db_session):
    """Milestone 17: jurisdiction_id has no default -- omitting it must
    fail at the database level (NOT NULL), not silently succeed."""
    issue = Issue(
        original_text="Complaint text.",
        category=IssueCategory.OTHER,
        problem="Some problem.",
        severity=SeverityLevel.UNKNOWN,
        confidence=0.5,
    )
    db_session.add(issue)
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_jurisdiction_is_independent_of_assigned_department(db_session):
    """An issue can be jurisdiction-scoped while completely unassigned --
    this is the exact scenario the Milestone 17 bug fix addresses."""
    jid = _seed_jurisdiction_id(db_session)
    issue = create_issue_from_civic_issue(db_session, _sample_civic_issue(), jid)
    assert issue.jurisdiction_id == jid
    assert issue.assigned_department_id is None
    assert issue.assigned_department is None
