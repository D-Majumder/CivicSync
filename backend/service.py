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
from ai.schemas import IssueCategory, SeverityLevel
from backend.assignment_rules import DepartmentInactiveError, DepartmentNotFoundError
from backend.models import Department, Issue, IssueStatus
from backend.repository import (
    assign_department_to_issue,
    count_issues_by_department,
    count_issues_by_severity,
    count_issues_by_status,
    create_issue_from_civic_issue,
    get_assignment_history,
    get_department_by_code,
    get_department_issue_counts,
    get_issue_by_public_id,
    get_operational_queue as repo_get_operational_queue,
    get_stale_issues as repo_get_stale_issues,
    get_status_history,
    list_issues_for_admin,
    transition_issue_status,
)
from backend.schemas import (
    AdminAssignmentHistoryEntry,
    AdminIssueDetailResponse,
    AdminIssueListItem,
    AdminIssueListResponse,
    AdminStatusHistoryEntry,
    DashboardSummaryResponse,
    DepartmentIssueCount,
    DepartmentSummary,
    DepartmentSummaryResponse,
    PublicIssueTrackingResponse,
    PublicTimelineEntry,
)


def _status_label(status_value: IssueStatus) -> str:
    """Citizen/authority-friendly label for a status, e.g. "In Progress"
    for IN_PROGRESS. IssueStatus itself is never changed by this."""
    return status_value.value.replace("_", " ").title()


def _department_summary(department: Department | None) -> DepartmentSummary | None:
    """Build a DepartmentSummary from an Issue.assigned_department
    relationship value, or None if the issue isn't currently assigned."""
    if department is None:
        return None
    return DepartmentSummary(code=department.code, name=department.name)


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

    assigned_department = _department_summary(issue.assigned_department)

    return PublicIssueTrackingResponse(
        public_id=issue.public_id,
        category=issue.category,
        problem=issue.problem,
        location=issue.location,
        duration=issue.duration,
        affected_population=issue.affected_population,
        severity=issue.severity,
        status=issue.status,
        status_label=_status_label(issue.status),
        assigned_department=assigned_department,
        created_at=issue.created_at,
        updated_at=issue.updated_at,
        timeline=timeline,
    )


# --- Authority operations (Milestone 8) --------------------------------------


def _build_admin_list_item(issue: Issue) -> AdminIssueListItem:
    return AdminIssueListItem(
        public_id=issue.public_id,
        category=issue.category,
        problem=issue.problem,
        severity=issue.severity,
        status=issue.status,
        status_label=_status_label(issue.status),
        assigned_department=_department_summary(issue.assigned_department),
        created_at=issue.created_at,
        updated_at=issue.updated_at,
    )


def list_admin_issues(
    db: Session,
    *,
    status: IssueStatus | None = None,
    department_code: str | None = None,
    severity: SeverityLevel | None = None,
    category: IssueCategory | None = None,
    sort_by: str = "updated_at",
    sort_order: str = "desc",
    limit: int = 20,
    offset: int = 0,
) -> AdminIssueListResponse:
    """Build the paginated, filtered authority issue list. Never calls
    Gemini -- pure read over already-persisted issues."""
    items, total = list_issues_for_admin(
        db,
        status=status,
        department_code=department_code,
        severity=severity,
        category=category,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
    )
    return AdminIssueListResponse(
        items=[_build_admin_list_item(issue) for issue in items],
        total=total,
        limit=limit,
        offset=offset,
    )


def get_admin_issue_detail(db: Session, public_id: str) -> AdminIssueDetailResponse | None:
    """Build the full authority-facing issue detail, including status and
    assignment history. Returns None if no Issue matches public_id (route
    -> 404). Never calls Gemini."""
    issue = get_issue_by_public_id(db, public_id)
    if issue is None:
        return None

    status_history = [
        AdminStatusHistoryEntry(
            from_status=entry.from_status,
            to_status=entry.to_status,
            reason=entry.reason,
            changed_at=entry.changed_at,
        )
        for entry in get_status_history(db, issue)
    ]
    assignment_history = [
        AdminAssignmentHistoryEntry(
            department_code=entry.department.code,
            department_name=entry.department.name,
            assigned_at=entry.assigned_at,
            unassigned_at=entry.unassigned_at,
            reason=entry.reason,
        )
        for entry in get_assignment_history(db, issue)
    ]

    return AdminIssueDetailResponse(
        public_id=issue.public_id,
        original_text=issue.original_text,
        category=issue.category,
        problem=issue.problem,
        location=issue.location,
        duration=issue.duration,
        affected_population=issue.affected_population,
        suggested_department=issue.suggested_department,
        confidence=issue.confidence,
        severity=issue.severity,
        assigned_department=_department_summary(issue.assigned_department),
        status=issue.status,
        status_label=_status_label(issue.status),
        resolution_summary=issue.resolution_summary,
        created_at=issue.created_at,
        updated_at=issue.updated_at,
        resolved_at=issue.resolved_at,
        closed_at=issue.closed_at,
        status_history=status_history,
        assignment_history=assignment_history,
    )


def get_dashboard_summary(db: Session) -> DashboardSummaryResponse:
    """Build the dashboard aggregate-count summary. Every count comes from
    a SQL GROUP BY query (see backend.repository) -- this function never
    iterates raw Issue rows. Never calls Gemini."""
    by_status = count_issues_by_status(db)
    by_severity = count_issues_by_severity(db)
    by_department = [
        DepartmentIssueCount(code=department.code, name=department.name, count=count)
        for department, count in count_issues_by_department(db)
    ]
    return DashboardSummaryResponse(
        total_issues=sum(by_status.values()),
        by_status=by_status,
        by_severity=by_severity,
        by_department=by_department,
    )


def get_department_summary(db: Session, department_code: str) -> DepartmentSummaryResponse | None:
    """Build a single department's operational summary. Returns None if
    department_code doesn't match any department (route -> 404). Never
    calls Gemini."""
    department = get_department_by_code(db, department_code)
    if department is None:
        return None

    counts = get_department_issue_counts(db, department)
    return DepartmentSummaryResponse(
        code=department.code,
        name=department.name,
        total_assigned_issues=counts["total_assigned_issues"],
        by_status=counts["by_status"],
        by_severity=counts["by_severity"],
        active_issues=counts["active_issues"],
        resolved_issues=counts["resolved_issues"],
        closed_issues=counts["closed_issues"],
    )


def list_operational_queue(
    db: Session,
    *,
    department_code: str | None = None,
    severity: SeverityLevel | None = None,
    limit: int = 20,
    offset: int = 0,
) -> AdminIssueListResponse:
    """Build the operational queue (excludes CLOSED/REJECTED, ordered by
    severity priority then oldest updated_at). Never calls Gemini."""
    items, total = repo_get_operational_queue(
        db, department_code=department_code, severity=severity, limit=limit, offset=offset
    )
    return AdminIssueListResponse(
        items=[_build_admin_list_item(issue) for issue in items],
        total=total,
        limit=limit,
        offset=offset,
    )


def list_stale_issues(
    db: Session,
    *,
    older_than_hours: int = 48,
    department_code: str | None = None,
    status: IssueStatus | None = None,
    limit: int = 20,
    offset: int = 0,
) -> AdminIssueListResponse:
    """Build the stale/aging-issues list. Read-only. Never calls Gemini."""
    items, total = repo_get_stale_issues(
        db,
        older_than_hours=older_than_hours,
        department_code=department_code,
        status=status,
        limit=limit,
        offset=offset,
    )
    return AdminIssueListResponse(
        items=[_build_admin_list_item(issue) for issue in items],
        total=total,
        limit=limit,
        offset=offset,
    )
