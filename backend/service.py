"""
Service layer for civic issue submission.

Orchestrates the existing AI service (ai.client.analyze_complaint) and the
existing persistence layer (backend.repository), so routes stay thin and
this business logic isn't duplicated across handlers or tests. This is the
"Service" box in:

    API -> Service -> AI
                    -> Database
"""

from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ai.client import analyze_complaint
from backend.assignment_rules import DepartmentInactiveError, DepartmentNotFoundError
from backend.models import Issue, IssueStatus
from backend.repository import (
    assign_department_to_issue,
    create_issue_from_civic_issue,
    get_department_by_code,
    get_issue_by_public_id,
    get_status_history,
    transition_issue_status,
)
from backend.schemas import DepartmentSummary, PublicIssueTrackingResponse, PublicTimelineEntry


def submit_complaint(db: Session, text: str) -> Issue:
    """Analyze citizen complaint text and persist the result as an Issue.

    Exceptions from analyze_complaint (ValueError, EnvironmentError,
    google.genai.errors.APIError) propagate unchanged -- the route already
    knows how to translate those into sanitized HTTP responses for
    POST /api/analyze, and does the same here. On a persistence failure,
    the session is rolled back before the SQLAlchemyError is re-raised, so
    the route (and the request's session) are left in a clean state.
    """
    civic_issue = analyze_complaint(text)

    try:
        return create_issue_from_civic_issue(db, civic_issue)
    except SQLAlchemyError:
        db.rollback()
        raise


def transition_issue(
    db: Session, public_id: str, to_status: IssueStatus, reason: str | None = None
) -> Issue | None:
    """Look up an Issue by public_id and apply a validated status transition.

    Returns None if no Issue matches public_id (the route maps that to
    404). Raises InvalidTransitionError (from backend.transitions) if the
    requested transition isn't permitted -- the route maps that to 400.
    On a persistence failure, the session is rolled back before the
    SQLAlchemyError is re-raised (the route maps that to a sanitized 500).
    """
    issue = get_issue_by_public_id(db, public_id)
    if issue is None:
        return None

    try:
        return transition_issue_status(db, issue, to_status, reason)
    except SQLAlchemyError:
        db.rollback()
        raise


def assign_issue_department(
    db: Session, public_id: str, department_code: str, reason: str | None = None
) -> Issue | None:
    """Look up an Issue and Department, then apply an official assignment.

    Returns None if no Issue matches public_id (route -> 404).
    Raises DepartmentNotFoundError if department_code doesn't match any
    department (route -> 404). Raises DepartmentInactiveError if the
    department exists but is inactive (route -> 400). Raises
    AssignmentNotAllowedError (from backend.assignment_rules) if the
    issue's status doesn't permit assignment (route -> 400). Raises
    SQLAlchemyError on persistence failure, after rolling back (route ->
    sanitized 500). Never calls Gemini.
    """
    issue = get_issue_by_public_id(db, public_id)
    if issue is None:
        return None

    department = get_department_by_code(db, department_code)
    if department is None:
        raise DepartmentNotFoundError(department_code)
    if not department.is_active:
        raise DepartmentInactiveError(department_code)

    try:
        return assign_department_to_issue(db, issue, department, reason)
    except SQLAlchemyError:
        db.rollback()
        raise


def get_public_tracking(db: Session, public_id: str) -> PublicIssueTrackingResponse | None:
    """Build the public, citizen-safe tracking representation for an Issue.

    Returns None if no Issue matches public_id (route -> 404). Never calls
    Gemini -- the issue is already analyzed and persisted, and this is a
    pure read.

    This is where the privacy boundary actually lives: the response is
    built field-by-field from the Issue (and its status history), never by
    handing the ORM object to a response_model with from_attributes=True
    the way the internal/admin endpoints do. That means a future field
    added to Issue can never silently leak into this public response --
    someone has to deliberately add it here.
    """
    issue = get_issue_by_public_id(db, public_id)
    if issue is None:
        return None

    # Citizen timeline: to_status/changed_at/reason only. from_status is
    # never exposed, including the initial history row's from_status=None
    # -- that row simply becomes a "SUBMITTED" timeline entry.
    timeline = [
        PublicTimelineEntry(
            status=entry.to_status,
            timestamp=entry.changed_at,
            reason=entry.reason,
        )
        for entry in get_status_history(db, issue)
    ]

    assigned_department = None
    if issue.assigned_department is not None:
        assigned_department = DepartmentSummary(
            code=issue.assigned_department.code,
            name=issue.assigned_department.name,
        )

    return PublicIssueTrackingResponse(
        public_id=issue.public_id,
        category=issue.category,
        problem=issue.problem,
        location=issue.location,
        duration=issue.duration,
        affected_population=issue.affected_population,
        severity=issue.severity,
        status=issue.status,
        status_label=issue.status.value.replace("_", " ").title(),
        assigned_department=assigned_department,
        created_at=issue.created_at,
        updated_at=issue.updated_at,
        timeline=timeline,
    )
