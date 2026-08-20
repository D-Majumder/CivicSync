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
from backend.models import Issue, IssueStatus
from backend.repository import (
    create_issue_from_civic_issue,
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
