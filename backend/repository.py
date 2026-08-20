"""
Service-layer functions for persisting and retrieving Issue records.

This is intentionally small -- a full CRUD API is a later task. It exists
so persistence logic has one home (not duplicated across tests or, later,
routes), consistent with CivicSync's API -> Service -> Database layering.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func
from sqlalchemy.orm import Session, joinedload

from ai.schemas import CivicIssue, IssueCategory, SeverityLevel
from backend.assignment_rules import check_assignment_allowed
from backend.models import (
    Department,
    Issue,
    IssueAssignmentHistory,
    IssueStatus,
    IssueStatusHistory,
)
from backend.transitions import validate_transition

# Terminal lifecycle states -- an issue in one of these is done, in either
# direction (successfully closed, or rejected as not actionable). Reused
# by the department summary's "active issues" calculation and the
# operational queue's exclusion list below.
TERMINAL_STATUSES: frozenset[IssueStatus] = frozenset({IssueStatus.CLOSED, IssueStatus.REJECTED})

# Statuses that represent work still requiring operational attention --
# everything except the terminal states above.
OPERATIONAL_STATUSES: frozenset[IssueStatus] = frozenset(
    s for s in IssueStatus if s not in TERMINAL_STATUSES
)

# Explicit, deterministic severity ordering for the operational queue --
# NOT alphabetical. Lower number = higher priority (surfaced first).
_SEVERITY_PRIORITY_CASE = case(
    (Issue.severity == SeverityLevel.CRITICAL, 0),
    (Issue.severity == SeverityLevel.HIGH, 1),
    (Issue.severity == SeverityLevel.MEDIUM, 2),
    (Issue.severity == SeverityLevel.LOW, 3),
    (Issue.severity == SeverityLevel.UNKNOWN, 4),
    else_=5,
)


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


# --- Authority operations (Milestone 8) --------------------------------------
#
# Everything below is read-only and backs the internal authority/dashboard
# API in backend/main.py. All filtering, sorting, pagination, and counting
# happens in SQL -- never by loading the full issues table into Python.


def list_issues_for_admin(
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
) -> tuple[list[Issue], int]:
    """Filtered, sorted, paginated issue list for GET /api/admin/issues.

    Filters are ANDed together. department_code filters by the OFFICIAL
    assigned department (Issue.assigned_department), never
    suggested_department. Returns (items, total) where total is the count
    matching the filters BEFORE limit/offset are applied.
    """
    query = db.query(Issue).options(joinedload(Issue.assigned_department))

    if status is not None:
        query = query.filter(Issue.status == status)
    if department_code is not None:
        # A correlated EXISTS rather than an explicit join, so this filter
        # doesn't interact with the joinedload() above (which already adds
        # its own join for eager-loading assigned_department).
        query = query.filter(Issue.assigned_department.has(Department.code == department_code))
    if severity is not None:
        query = query.filter(Issue.severity == severity)
    if category is not None:
        query = query.filter(Issue.category == category)

    # Count before pagination. Safe to count with joinedload() attached
    # here because assigned_department is many-to-one -- the eager-load
    # join can't multiply rows.
    total = query.count()

    sort_columns = {
        "updated_at": Issue.updated_at,
        "created_at": Issue.created_at,
        "severity": _SEVERITY_PRIORITY_CASE,
    }
    sort_column = sort_columns.get(sort_by, Issue.updated_at)
    query = query.order_by(sort_column.asc() if sort_order == "asc" else sort_column.desc())

    items = query.limit(limit).offset(offset).all()
    return items, total


def count_issues_by_status(db: Session) -> dict[IssueStatus, int]:
    """Issue counts grouped by status, via SQL GROUP BY. Every IssueStatus
    is present in the result, zero-filled if no issues currently have it."""
    counts: dict[IssueStatus, int] = {s: 0 for s in IssueStatus}
    rows = db.query(Issue.status, func.count(Issue.id)).group_by(Issue.status).all()
    for status_value, count in rows:
        counts[status_value] = count
    return counts


def count_issues_by_severity(db: Session) -> dict[SeverityLevel, int]:
    """Issue counts grouped by severity, via SQL GROUP BY. Every
    SeverityLevel is present in the result, zero-filled if unused."""
    counts: dict[SeverityLevel, int] = {s: 0 for s in SeverityLevel}
    rows = db.query(Issue.severity, func.count(Issue.id)).group_by(Issue.severity).all()
    for severity_value, count in rows:
        counts[severity_value] = count
    return counts


def count_issues_by_department(db: Session) -> list[tuple[Department, int]]:
    """Every active Department paired with its currently-assigned issue
    count (0 if none), via a single LEFT JOIN + GROUP BY -- no N+1."""
    return (
        db.query(Department, func.count(Issue.id))
        .outerjoin(Issue, Issue.assigned_department_id == Department.id)
        .filter(Department.is_active.is_(True))
        .group_by(Department.id)
        .order_by(Department.code)
        .all()
    )


def get_department_issue_counts(db: Session, department: Department) -> dict:
    """Aggregate counts for a single department's currently-assigned
    issues: total, by status, by severity, and active/resolved/closed.
    All counts come from SQL GROUP BY; "active" is a small Python sum over
    the resulting 9-status dict, not a scan of raw issue rows.
    """
    status_rows = (
        db.query(Issue.status, func.count(Issue.id))
        .filter(Issue.assigned_department_id == department.id)
        .group_by(Issue.status)
        .all()
    )
    by_status: dict[IssueStatus, int] = {s: 0 for s in IssueStatus}
    for status_value, count in status_rows:
        by_status[status_value] = count

    severity_rows = (
        db.query(Issue.severity, func.count(Issue.id))
        .filter(Issue.assigned_department_id == department.id)
        .group_by(Issue.severity)
        .all()
    )
    by_severity: dict[SeverityLevel, int] = {s: 0 for s in SeverityLevel}
    for severity_value, count in severity_rows:
        by_severity[severity_value] = count

    total = sum(by_status.values())
    active = sum(count for st, count in by_status.items() if st not in TERMINAL_STATUSES)

    return {
        "total_assigned_issues": total,
        "by_status": by_status,
        "by_severity": by_severity,
        "active_issues": active,
        "resolved_issues": by_status[IssueStatus.RESOLVED],
        "closed_issues": by_status[IssueStatus.CLOSED],
    }


def get_operational_queue(
    db: Session,
    *,
    department_code: str | None = None,
    severity: SeverityLevel | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Issue], int]:
    """Issues requiring operational attention (excludes CLOSED, REJECTED),
    ordered by severity priority (CRITICAL first) then oldest updated_at
    first -- both computed in SQL, not Python. Returns (items, total)."""
    query = (
        db.query(Issue)
        .options(joinedload(Issue.assigned_department))
        .filter(Issue.status.in_(OPERATIONAL_STATUSES))
    )
    if department_code is not None:
        query = query.filter(Issue.assigned_department.has(Department.code == department_code))
    if severity is not None:
        query = query.filter(Issue.severity == severity)

    total = query.count()
    items = (
        query.order_by(_SEVERITY_PRIORITY_CASE.asc(), Issue.updated_at.asc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    return items, total


def get_stale_issues(
    db: Session,
    *,
    older_than_hours: int = 48,
    department_code: str | None = None,
    status: IssueStatus | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Issue], int]:
    """Issues whose updated_at is at least older_than_hours old. Read-only
    -- never mutates anything. Returns (items, total), oldest-updated first.

    Issue.updated_at is stored as a naive UTC datetime (SQLite's DATETIME
    columns don't persist a timezone offset), so the cutoff below is
    computed in UTC and then made naive to compare correctly -- comparing
    a naive column against an aware datetime would either raise or compare
    incorrectly depending on the DB-API driver.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=older_than_hours)).replace(
        tzinfo=None
    )

    query = (
        db.query(Issue)
        .options(joinedload(Issue.assigned_department))
        .filter(Issue.updated_at <= cutoff)
    )
    if department_code is not None:
        query = query.filter(Issue.assigned_department.has(Department.code == department_code))
    if status is not None:
        query = query.filter(Issue.status == status)

    total = query.count()
    items = query.order_by(Issue.updated_at.asc()).limit(limit).offset(offset).all()
    return items, total
