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
from backend.models import Issue
from backend.repository import create_issue_from_civic_issue


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
