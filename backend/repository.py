"""
Service-layer functions for persisting and retrieving Issue records.

This is intentionally small -- a full CRUD API is a later task. It exists
so persistence logic has one home (not duplicated across tests or, later,
routes), consistent with CivicSync's API -> Service -> Database layering.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ai.schemas import CivicIssue
from backend.models import Issue, IssueStatus, IssueStatusHistory
from backend.transitions import validate_transition


def create_issue_from_civic_issue(db: Session, civic_issue: CivicIssue) -> Issue:
    """Persist a new Issue from a freshly AI-analyzed CivicIssue.

    Only fields CivicIssue actually produces are populated here. Official/
    operational fields (assigned_department, resolution_summary,
    resolved_at, closed_at) are left at their defaults -- AI output is
    extraction, not an operational decision, so it never sets those.

    Also writes the initial status history row (from_status=None,
    to_status=SUBMITTED) in the same transaction, so every Issue has a
    complete history from the moment it exists.
    """
    issue = Issue(
        original_text=civic_issue.original_text,
        category=civic_issue.category,
        problem=civic_issue.problem,
        location=civic_issue.location,
        duration=civic_issue.duration,
        severity=civic_issue.severity,
        affected_population=civic_issue.affected_population,
        suggested_department=civic_issue.suggested_department,
        confidence=civic_issue.confidence,
        status=IssueStatus.SUBMITTED,
    )
    db.add(issue)
    # Flush (not commit) so issue.id is assigned and available for the
    # history row's foreign key, while keeping both inserts in one
    # transaction.
    db.flush()

    db.add(
        IssueStatusHistory(
            issue_id=issue.id,
            from_status=None,
            to_status=issue.status,
            reason="Issue submitted.",
        )
    )

    db.commit()
    db.refresh(issue)
    return issue


def get_issue_by_public_id(db: Session, public_id: str) -> Issue | None:
    """Fetch a single Issue by its public-facing identifier, or None."""
    return db.query(Issue).filter(Issue.public_id == public_id).one_or_none()


def transition_issue_status(
    db: Session, issue: Issue, to_status: IssueStatus, reason: str | None = None
) -> Issue:
    """Validate and apply a single status transition, atomically.

    Raises InvalidTransitionError (from backend.transitions) if the
    transition isn't permitted -- in that case nothing is written. On
    success, updates Issue.status (and resolved_at/closed_at where
    applicable) and writes one IssueStatusHistory row, committed together.
    Raises SQLAlchemyError on persistence failure; the caller (service
    layer) is responsible for rolling back the session.
    """
    validate_transition(issue.status, to_status)

    from_status = issue.status
    now = datetime.now(timezone.utc)

    issue.status = to_status
    if to_status == IssueStatus.RESOLVED:
        # Entering RESOLVED: record when, but never touch closed_at here.
        issue.resolved_at = now
    elif to_status == IssueStatus.CLOSED:
        # Entering CLOSED: record when. resolved_at was already set by the
        # prior IN_PROGRESS -> RESOLVED transition and is left untouched.
        issue.closed_at = now
    # REOPENED and every other target status intentionally leave
    # resolved_at/closed_at untouched -- REOPENED preserves the original
    # resolved_at for historical accuracy, and REOPENED -> IN_PROGRESS
    # must not create a new one.

    db.add(
        IssueStatusHistory(
            issue_id=issue.id,
            from_status=from_status,
            to_status=to_status,
            reason=reason,
        )
    )

    db.commit()
    db.refresh(issue)
    return issue


def get_status_history(db: Session, issue: Issue) -> list[IssueStatusHistory]:
    """Return an Issue's status history, oldest first."""
    return (
        db.query(IssueStatusHistory)
        .filter(IssueStatusHistory.issue_id == issue.id)
        .order_by(IssueStatusHistory.changed_at.asc(), IssueStatusHistory.id.asc())
        .all()
    )
