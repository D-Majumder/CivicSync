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
from backend.assignment_rules import check_assignment_allowed
from backend.models import (
    Department,
    Issue,
    IssueAssignmentHistory,
    IssueStatus,
    IssueStatusHistory,
)
from backend.transitions import validate_transition


def create_issue_from_civic_issue(db: Session, civic_issue: CivicIssue) -> Issue:
    """Persist a new Issue from a freshly AI-analyzed CivicIssue.

    Only fields CivicIssue actually produces are populated here. Official/
    operational fields (assigned_department_id, resolution_summary,
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


def _apply_validated_status_transition(
    db: Session, issue: Issue, to_status: IssueStatus, reason: str | None = None
) -> None:
    """Validate and stage a single status transition -- WITHOUT committing.

    Updates Issue.status (and resolved_at/closed_at where applicable) and
    stages one IssueStatusHistory row. Callers control the transaction
    boundary (db.commit()), so this can be combined atomically with other
    changes in the same transaction (e.g. a department assignment).
    Raises InvalidTransitionError (from backend.transitions) if the
    transition isn't permitted -- in that case nothing is staged.
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
    _apply_validated_status_transition(db, issue, to_status, reason)
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


# --- Department registry ----------------------------------------------------


def get_active_departments(db: Session) -> list[Department]:
    """Return active departments from the controlled registry, by code."""
    return (
        db.query(Department)
        .filter(Department.is_active.is_(True))
        .order_by(Department.code)
        .all()
    )


def get_department_by_code(db: Session, code: str) -> Department | None:
    """Fetch a single Department by its code (active or not), or None."""
    return db.query(Department).filter(Department.code == code).one_or_none()


# --- Official department assignment -----------------------------------------


def get_current_assignment(db: Session, issue: Issue) -> IssueAssignmentHistory | None:
    """Return the Issue's currently-active assignment row, or None if it
    has never been assigned (or its assignment was closed without a
    replacement, which the API never actually does today)."""
    return (
        db.query(IssueAssignmentHistory)
        .filter(
            IssueAssignmentHistory.issue_id == issue.id,
            IssueAssignmentHistory.unassigned_at.is_(None),
        )
        .order_by(IssueAssignmentHistory.assigned_at.desc())
        .first()
    )


def get_assignment_history(db: Session, issue: Issue) -> list[IssueAssignmentHistory]:
    """Return an Issue's full assignment history, oldest first."""
    return (
        db.query(IssueAssignmentHistory)
        .filter(IssueAssignmentHistory.issue_id == issue.id)
        .order_by(IssueAssignmentHistory.assigned_at.asc(), IssueAssignmentHistory.id.asc())
        .all()
    )


def assign_department_to_issue(
    db: Session, issue: Issue, department: Department, reason: str | None = None
) -> Issue:
    """Assign (or reassign) a Department to an Issue, atomically.

    Raises AssignmentNotAllowedError (from backend.assignment_rules) if
    the issue's current status doesn't permit assignment -- in that case
    nothing is written. If the issue is CLASSIFIED, this also transitions
    it to ROUTED via the existing centralized transition system (see
    backend.transitions / _apply_validated_status_transition above), in
    the SAME transaction as the assignment -- one commit covers closing
    any previous assignment, the new assignment history row, the FK
    update on Issue, and (when applicable) the status change + its
    history row. Raises SQLAlchemyError on persistence failure; the
    caller (service layer) is responsible for rolling back.
    """
    check_assignment_allowed(issue.status)

    now = datetime.now(timezone.utc)

    # Reassignment: close out any currently-active assignment first.
    # Never delete it -- this is what makes the assignment history an
    # audit trail rather than just current state.
    current = get_current_assignment(db, issue)
    if current is not None:
        current.unassigned_at = now

    db.add(
        IssueAssignmentHistory(
            issue_id=issue.id,
            department_id=department.id,
            assigned_at=now,
            reason=reason,
        )
    )
    issue.assigned_department_id = department.id

    if issue.status == IssueStatus.CLASSIFIED:
        _apply_validated_status_transition(
            db,
            issue,
            IssueStatus.ROUTED,
            reason="Routed to department via official assignment.",
        )
    # ROUTED-or-later statuses (ACKNOWLEDGED, IN_PROGRESS, RESOLVED,
    # CLOSED, REOPENED) are assignable per check_assignment_allowed() above
    # but intentionally do NOT trigger any status change here.

    db.commit()
    db.refresh(issue)
    return issue
