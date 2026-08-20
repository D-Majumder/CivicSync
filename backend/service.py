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
    transition_issue_status,
)


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
