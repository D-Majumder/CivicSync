"""
Tests for Issue lifecycle management (status transitions + history) at the
repository/service layer. Uses the `db_session` fixture from
tests/conftest.py -- a temporary SQLite database, never civicsync.db. No
Gemini calls anywhere in this file.
"""

from unittest.mock import patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from ai.schemas import CivicIssue, IssueCategory, SeverityLevel
from backend.models import Issue, IssueStatus
from backend.repository import (
    create_issue_from_civic_issue,
    get_issue_by_public_id,
    get_status_history,
    transition_issue_status,
)
from backend.service import transition_issue
from backend.transitions import InvalidTransitionError


def _sample_civic_issue(**overrides) -> CivicIssue:
    base = dict(
        original_text="There has been no street light near our school for two weeks.",
        category=IssueCategory.STREET_LIGHTING,
        problem="Non-functional street light near a school.",
        severity=SeverityLevel.MEDIUM,
        confidence=0.8,
    )
    base.update(overrides)
    return CivicIssue(**base)


def _create_issue(db_session) -> Issue:
    return create_issue_from_civic_issue(db_session, _sample_civic_issue())


def _advance(db_session, issue: Issue, *targets: IssueStatus) -> Issue:
    for target in targets:
        issue = transition_issue_status(db_session, issue, target)
    return issue


# --- 1 & 2: new issue starts SUBMITTED, with initial history ----------------


def test_new_issue_starts_submitted(db_session):
    issue = _create_issue(db_session)
    assert issue.status == IssueStatus.SUBMITTED


def test_initial_submitted_history_record_exists(db_session):
    """Verifies the exact required initial record: from_status=None,
    to_status=SUBMITTED, reason="Issue submitted." -- not just presence."""
    issue = _create_issue(db_session)
    history = get_status_history(db_session, issue)
    assert len(history) == 1
    assert history[0].from_status is None
    assert history[0].to_status == IssueStatus.SUBMITTED
    assert history[0].reason == "Issue submitted."
    assert history[0].changed_at is not None


def test_initial_submitted_history_record_visible_via_relationship(db_session):
    """Same check via issue.status_history (the ORM relationship), since
    that's exactly how the missing-initial-record issue was reported."""
    issue = _create_issue(db_session)
    assert len(issue.status_history) == 1
    assert issue.status_history[0].from_status is None
    assert issue.status_history[0].to_status == IssueStatus.SUBMITTED
    assert issue.status_history[0].reason == "Issue submitted."


# --- 3-11: each lifecycle transition succeeds --------------------------------


def test_submitted_to_classified_succeeds(db_session):
    issue = _create_issue(db_session)
    updated = transition_issue_status(db_session, issue, IssueStatus.CLASSIFIED)
    assert updated.status == IssueStatus.CLASSIFIED


def test_classified_to_routed_succeeds(db_session):
    issue = _create_issue(db_session)
    _advance(db_session, issue, IssueStatus.CLASSIFIED)
    updated = transition_issue_status(db_session, issue, IssueStatus.ROUTED)
    assert updated.status == IssueStatus.ROUTED


def test_routed_to_acknowledged_succeeds(db_session):
    issue = _create_issue(db_session)
    _advance(db_session, issue, IssueStatus.CLASSIFIED, IssueStatus.ROUTED)
    updated = transition_issue_status(db_session, issue, IssueStatus.ACKNOWLEDGED)
    assert updated.status == IssueStatus.ACKNOWLEDGED


def test_acknowledged_to_in_progress_succeeds(db_session):
    issue = _create_issue(db_session)
    _advance(
        db_session, issue, IssueStatus.CLASSIFIED, IssueStatus.ROUTED, IssueStatus.ACKNOWLEDGED
    )
    updated = transition_issue_status(db_session, issue, IssueStatus.IN_PROGRESS)
    assert updated.status == IssueStatus.IN_PROGRESS


def test_in_progress_to_resolved_succeeds(db_session):
    issue = _create_issue(db_session)
    _advance(
        db_session,
        issue,
        IssueStatus.CLASSIFIED,
        IssueStatus.ROUTED,
        IssueStatus.ACKNOWLEDGED,
        IssueStatus.IN_PROGRESS,
    )
    updated = transition_issue_status(db_session, issue, IssueStatus.RESOLVED)
    assert updated.status == IssueStatus.RESOLVED


def test_resolved_to_closed_succeeds(db_session):
    issue = _create_issue(db_session)
    _advance(
        db_session,
        issue,
        IssueStatus.CLASSIFIED,
        IssueStatus.ROUTED,
        IssueStatus.ACKNOWLEDGED,
        IssueStatus.IN_PROGRESS,
        IssueStatus.RESOLVED,
    )
    updated = transition_issue_status(db_session, issue, IssueStatus.CLOSED)
    assert updated.status == IssueStatus.CLOSED


def test_submitted_to_rejected_succeeds(db_session):
    issue = _create_issue(db_session)
    updated = transition_issue_status(db_session, issue, IssueStatus.REJECTED)
    assert updated.status == IssueStatus.REJECTED


def test_resolved_to_reopened_succeeds(db_session):
    issue = _create_issue(db_session)
    _advance(
        db_session,
        issue,
        IssueStatus.CLASSIFIED,
        IssueStatus.ROUTED,
        IssueStatus.ACKNOWLEDGED,
        IssueStatus.IN_PROGRESS,
        IssueStatus.RESOLVED,
    )
    updated = transition_issue_status(db_session, issue, IssueStatus.REOPENED)
    assert updated.status == IssueStatus.REOPENED


def test_reopened_to_in_progress_succeeds(db_session):
    issue = _create_issue(db_session)
    _advance(
        db_session,
        issue,
        IssueStatus.CLASSIFIED,
        IssueStatus.ROUTED,
        IssueStatus.ACKNOWLEDGED,
        IssueStatus.IN_PROGRESS,
        IssueStatus.RESOLVED,
        IssueStatus.REOPENED,
    )
    updated = transition_issue_status(db_session, issue, IssueStatus.IN_PROGRESS)
    assert updated.status == IssueStatus.IN_PROGRESS


# --- 12-14: invalid transitions rejected -------------------------------------


def test_invalid_transition_is_rejected(db_session):
    issue = _create_issue(db_session)
    with pytest.raises(InvalidTransitionError):
        transition_issue_status(db_session, issue, IssueStatus.RESOLVED)


def test_closed_cannot_transition_anywhere(db_session):
    issue = _create_issue(db_session)
    _advance(
        db_session,
        issue,
        IssueStatus.CLASSIFIED,
        IssueStatus.ROUTED,
        IssueStatus.ACKNOWLEDGED,
        IssueStatus.IN_PROGRESS,
        IssueStatus.RESOLVED,
        IssueStatus.CLOSED,
    )
    with pytest.raises(InvalidTransitionError):
        transition_issue_status(db_session, issue, IssueStatus.IN_PROGRESS)
    with pytest.raises(InvalidTransitionError):
        transition_issue_status(db_session, issue, IssueStatus.RESOLVED)


def test_submitted_cannot_jump_to_resolved(db_session):
    issue = _create_issue(db_session)
    with pytest.raises(InvalidTransitionError):
        transition_issue_status(db_session, issue, IssueStatus.RESOLVED)


# --- 15-16: status history created only for valid transitions ---------------


def test_status_history_created_for_valid_transition(db_session):
    issue = _create_issue(db_session)
    transition_issue_status(
        db_session, issue, IssueStatus.CLASSIFIED, reason="Validated civic issue"
    )
    history = get_status_history(db_session, issue)
    assert len(history) == 2
    assert history[1].from_status == IssueStatus.SUBMITTED
    assert history[1].to_status == IssueStatus.CLASSIFIED
    assert history[1].reason == "Validated civic issue"


def test_invalid_transition_does_not_create_history_entry(db_session):
    issue = _create_issue(db_session)
    before_count = len(get_status_history(db_session, issue))
    before_status = issue.status

    with pytest.raises(InvalidTransitionError):
        transition_issue_status(db_session, issue, IssueStatus.RESOLVED)

    assert issue.status == before_status
    assert len(get_status_history(db_session, issue)) == before_count


# --- 17-19: resolution timestamps --------------------------------------------


def test_resolved_at_set_when_entering_resolved(db_session):
    issue = _create_issue(db_session)
    _advance(
        db_session,
        issue,
        IssueStatus.CLASSIFIED,
        IssueStatus.ROUTED,
        IssueStatus.ACKNOWLEDGED,
        IssueStatus.IN_PROGRESS,
    )
    assert issue.resolved_at is None

    transition_issue_status(db_session, issue, IssueStatus.RESOLVED)
    assert issue.resolved_at is not None
    assert issue.closed_at is None


def test_closed_at_set_when_entering_closed(db_session):
    issue = _create_issue(db_session)
    _advance(
        db_session,
        issue,
        IssueStatus.CLASSIFIED,
        IssueStatus.ROUTED,
        IssueStatus.ACKNOWLEDGED,
        IssueStatus.IN_PROGRESS,
        IssueStatus.RESOLVED,
    )
    assert issue.closed_at is None

    transition_issue_status(db_session, issue, IssueStatus.CLOSED)
    assert issue.closed_at is not None


def test_resolved_at_preserved_after_reopened_and_in_progress(db_session):
    issue = _create_issue(db_session)
    _advance(
        db_session,
        issue,
        IssueStatus.CLASSIFIED,
        IssueStatus.ROUTED,
        IssueStatus.ACKNOWLEDGED,
        IssueStatus.IN_PROGRESS,
        IssueStatus.RESOLVED,
    )
    original_resolved_at = issue.resolved_at
    assert original_resolved_at is not None

    transition_issue_status(db_session, issue, IssueStatus.REOPENED)
    assert issue.resolved_at == original_resolved_at

    # REOPENED -> IN_PROGRESS must not create a *new* resolved_at either.
    transition_issue_status(db_session, issue, IssueStatus.IN_PROGRESS)
    assert issue.resolved_at == original_resolved_at


# --- Atomicity: a failed transition leaves status & history unchanged -------


def test_failed_transition_rolls_back_status_and_history(db_session):
    """Simulate a persistence failure at commit time and confirm neither
    the Issue's status nor a history row is left committed."""
    issue = _create_issue(db_session)
    public_id = issue.public_id
    original_status = issue.status
    original_history_count = len(get_status_history(db_session, issue))

    with patch.object(
        db_session, "commit", side_effect=SQLAlchemyError("simulated commit failure")
    ):
        with pytest.raises(SQLAlchemyError):
            transition_issue(db_session, public_id, IssueStatus.CLASSIFIED)

    # Force a fresh read from the database (not the in-memory Python object)
    # to prove nothing was actually persisted.
    db_session.expire_all()
    refreshed = get_issue_by_public_id(db_session, public_id)
    assert refreshed.status == original_status
    assert len(get_status_history(db_session, refreshed)) == original_history_count
