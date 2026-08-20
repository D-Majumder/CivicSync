"""
Service-layer functions for persisting and retrieving Issue records.

This is intentionally small -- a full CRUD API is a later task. It exists
so persistence logic has one home (not duplicated across tests or, later,
routes), consistent with CivicSync's API -> Service -> Database layering.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ai.schemas import CivicIssue
from backend.models import Issue, IssueStatus


def create_issue_from_civic_issue(db: Session, civic_issue: CivicIssue) -> Issue:
    """Persist a new Issue from a freshly AI-analyzed CivicIssue.

    Only fields CivicIssue actually produces are populated here. Official/
    operational fields (assigned_department, resolution_summary,
    resolved_at, closed_at) are left at their defaults -- AI output is
    extraction, not an operational decision, so it never sets those.
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
    db.commit()
    db.refresh(issue)
    return issue


def get_issue_by_public_id(db: Session, public_id: str) -> Issue | None:
    """Fetch a single Issue by its public-facing identifier, or None."""
    return db.query(Issue).filter(Issue.public_id == public_id).one_or_none()
