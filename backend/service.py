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

import os
import secrets
from datetime import datetime, timezone
from io import BytesIO

from google.genai.errors import APIError
from PIL import Image, UnidentifiedImageError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend import evidence_storage
from backend.hotspots import GeoIssuePoint, detect_hotspots

from ai.client import (
    analyze_complaint,
    generate_insight_prioritization,
    generate_issue_explanation,
    generate_operational_briefing,
)
from ai.schemas import IssueCategory, SeverityLevel
from backend import insights as insight_rules
from backend.assignment_rules import DepartmentInactiveError, DepartmentNotFoundError
from backend.models import Department, Issue, IssueStatus, JurisdictionLevel, ReopenRequest
from backend.repository import (
    METRIC_INACTIVE_STATUSES,
    TERMINAL_STATUSES,
    assign_department_to_issue,
    count_active_high_severity_issues,
    count_active_issues_by_category,
    count_issues_by_department,
    count_issues_by_severity,
    count_issues_by_status,
    create_issue_from_civic_issue,
    get_active_issues_for_briefing,
    get_default_jurisdiction_id,
    get_assignment_history,
    get_department_by_code,
    get_department_issue_counts,
    get_department_workload,
    get_issue_aging_buckets,
    get_issue_by_public_id,
    get_jurisdiction_ancestry,
    get_jurisdiction_by_code,
    get_jurisdictions_with_ancestry,
    get_operational_queue as repo_get_operational_queue,
    get_recent_activity,
    get_geospatial_issues as repo_get_geospatial_issues,
    get_resolution_intelligence as repo_get_resolution_intelligence,
    get_resolution_timing_metrics,
    get_stale_issues as repo_get_stale_issues,
    get_status_history,
    list_issues_for_admin,
    create_evidence,
    create_reopen_request,
    decide_reopen_request as repo_decide_reopen_request,
    get_evidence_by_public_id,
    get_evidence_for_issue,
    get_issue_ids_with_pending_reopen_request,
    get_pending_reopen_request,
    get_reopen_request_by_public_id,
    resolve_issue as repo_resolve_issue,
    transition_issue_status,
)
from backend.schemas import (
    AdminAssignmentHistoryEntry,
    AdminIssueDetailResponse,
    AdminIssueListItem,
    AdminIssueListResponse,
    AdminStatusHistoryEntry,
    AgingBucketEntry,
    AgingBucketsResponse,
    AIPrioritizationRecommendation,
    DashboardSummaryResponse,
    DepartmentIssueCount,
    DepartmentSummary,
    DepartmentSummaryResponse,
    DepartmentWorkloadEntry,
    DepartmentWorkloadResponse,
    EvidenceResponse,
    FunnelResponse,
    FunnelStage,
    Insight,
    InsightsResponse,
    IssueExplanationResponse,
    JurisdictionListResponse,
    JurisdictionResponse,
    JurisdictionSummary,
    OperationalBriefingResponse,
    PrioritizedInsightsResponse,
    PublicEvidenceSummary,
    ReopenRequestResponse,
    PublicIssueTrackingResponse,
    PublicTimelineEntry,
    RecentActivityEntry,
    RecentActivityResponse,
    CategoryReopenCount,
    GeoIssueSummary,
    HotspotResponse,
    HotspotsResponse,
    ResolutionIntelligenceResponse,
    ResolutionTimingResponse,
    SeverityDistributionEntry,
    SeverityDistributionResponse,
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


def submit_complaint(
    db: Session,
    text: str,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    location_accuracy: float | None = None,
    citizen_language: str | None = None,
) -> Issue:
    """Analyze citizen complaint text and persist the result as an Issue.

    Every new Issue is scoped to the environment-configured default
    jurisdiction (Milestone 17) -- see
    backend.repository.get_default_jurisdiction_id. This is resolved
    FIRST, before calling Gemini, so a misconfigured deployment fails
    fast without spending an AI call it would have to discard anyway.

    latitude/longitude/location_accuracy (Milestone 21) and
    citizen_language (Milestone 22) are entirely optional and default to
    None -- passed straight through to create_issue_from_civic_issue,
    never affecting jurisdiction resolution or Gemini's analysis in any
    way. citizen_language is the citizen's own UI selection, never sent
    to Gemini as an instruction and never used to alter `text` itself --
    Gemini is instructed (see ai/prompts.py's SYSTEM_PROMPT) to tolerate
    English/Hindi/Bengali, spelling errors, and transliteration directly,
    without any language hint or preprocessing from this function.

    Exceptions from get_default_jurisdiction_id or analyze_complaint
    (ValueError, EnvironmentError, google.genai.errors.APIError)
    propagate unchanged -- the route already knows how to translate those
    into sanitized HTTP responses for POST /api/issues, and does the same
    here. On a persistence failure, the session is rolled back before the
    SQLAlchemyError is re-raised, so the route (and the request's
    session) are left in a clean state.
    """
    jurisdiction_id = get_default_jurisdiction_id(db)
    civic_issue = analyze_complaint(text)

    try:
        return create_issue_from_civic_issue(
            db,
            civic_issue,
            jurisdiction_id,
            latitude=latitude,
            longitude=longitude,
            location_accuracy=location_accuracy,
            citizen_language=citizen_language,
        )
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


def resolve_issue(
    db: Session, public_id: str, resolution_note: str, resolved_by: str
) -> Issue | None:
    """Look up an Issue by public_id and apply the authority resolution
    workflow (Milestone 18, Phase 2).

    Returns None if no Issue matches public_id (the route maps that to
    404). Raises InvalidTransitionError (from backend.transitions) if the
    issue isn't currently IN_PROGRESS -- the route maps that to 400,
    exactly like the generic status-transition endpoint. On a persistence
    failure, the session is rolled back before the SQLAlchemyError is
    re-raised (the route maps that to a sanitized 500).

    `resolved_by` must be the server-side authenticated authority
    identity (see backend/auth.py's get_current_authority) -- never
    accepted from request input.
    """
    issue = get_issue_by_public_id(db, public_id)
    if issue is None:
        return None

    try:
        return repo_resolve_issue(db, issue, resolution_note, resolved_by)
    except SQLAlchemyError:
        db.rollback()
        raise


# --- Citizen reopen requests (Milestone 18, Phase 5) --------------------------


class IssueNotResolvedError(Exception):
    """Raised when a reopen request is attempted against an issue that
    isn't currently RESOLVED. The route maps this to 400."""


class DuplicateReopenRequestError(Exception):
    """Raised when an issue already has a PENDING reopen request. The
    route maps this to 400."""


def request_issue_reopen(db: Session, public_id: str, reason: str) -> ReopenRequestResponse | None:
    """Citizen-facing: create a PENDING reopen request for a RESOLVED
    issue (Milestone 18, Phase 5).

    Returns None if no Issue matches public_id (route -> 404). Raises
    IssueNotResolvedError if the issue isn't currently RESOLVED, or
    DuplicateReopenRequestError if a PENDING request already exists for
    it -- both map to 400. This NEVER touches Issue.status -- creating a
    request is not itself a status change; see decide_reopen_request for
    the only path that can actually move the issue.
    """
    issue = get_issue_by_public_id(db, public_id)
    if issue is None:
        return None

    if issue.status != IssueStatus.RESOLVED:
        raise IssueNotResolvedError(
            "Reopening can only be requested for a RESOLVED issue."
        )
    if get_pending_reopen_request(db, issue) is not None:
        raise DuplicateReopenRequestError(
            "This issue already has a pending reopen request."
        )

    try:
        request = create_reopen_request(db, issue, reason)
    except SQLAlchemyError:
        db.rollback()
        raise

    return _to_reopen_request_response(request)


def get_issue_pending_reopen_request(db: Session, issue: Issue) -> ReopenRequestResponse | None:
    """Authority- and citizen-facing: the issue's current PENDING reopen
    request (or None), reused by both get_admin_issue_detail and
    get_public_tracking so the shape is identical everywhere it appears.
    """
    request = get_pending_reopen_request(db, issue)
    if request is None:
        return None
    return _to_reopen_request_response(request)


def decide_reopen_request(
    db: Session,
    request_public_id: str,
    *,
    approve: bool,
    decision_reason: str | None,
    decided_by: str,
) -> ReopenRequestResponse | None:
    """Authority-facing: approve or reject a reopen request (Milestone
    18, Phase 5).

    Returns None if no request matches request_public_id (route -> 404).
    Raises InvalidTransitionError (from backend.transitions) if approving
    and the issue is no longer RESOLVED -- the route maps that to 400,
    the same as every other illegal transition in this project. On a
    persistence failure, the session is rolled back before the
    SQLAlchemyError is re-raised.

    `decided_by` must be the server-side authenticated authority
    identity -- never accepted from request input. Citizens have no
    endpoint that can reach this function at all.
    """
    request = get_reopen_request_by_public_id(db, request_public_id)
    if request is None:
        return None

    try:
        decided = repo_decide_reopen_request(
            db,
            request,
            request.issue,
            approve=approve,
            decision_reason=decision_reason,
            decided_by=decided_by,
        )
    except SQLAlchemyError:
        db.rollback()
        raise

    return _to_reopen_request_response(decided)


def _to_reopen_request_response(request: ReopenRequest) -> ReopenRequestResponse:
    return ReopenRequestResponse(
        public_id=request.public_id,
        state=request.state,
        reason=request.citizen_reason,
        created_at=request.created_at,
        decision_reason=request.decision_reason,
        decided_at=request.decided_at,
    )


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
    evidence = [
        PublicEvidenceSummary(
            public_id=e.public_id,
            original_filename=e.original_filename,
            uploaded_at=e.uploaded_at,
        )
        for e in get_evidence_for_issue(db, issue)
    ]

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
        resolution_summary=issue.resolution_summary,
        resolved_at=issue.resolved_at,
        timeline=timeline,
        evidence=evidence,
        active_reopen_request=get_issue_pending_reopen_request(db, issue),
    )


# --- Authority operations (Milestone 8) --------------------------------------


def _build_admin_list_item(
    issue: Issue, *, pending_reopen_issue_ids: set[int] = frozenset()
) -> AdminIssueListItem:
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
        has_pending_reopen_request=issue.id in pending_reopen_issue_ids,
    )


def _build_admin_list_items(db: Session, issues: list[Issue]) -> list[AdminIssueListItem]:
    """Build a list of AdminIssueListItem, flagging pending reopen requests
    in one bulk query rather than one per row (see
    get_issue_ids_with_pending_reopen_request)."""
    pending_ids = get_issue_ids_with_pending_reopen_request(db, [issue.id for issue in issues])
    return [
        _build_admin_list_item(issue, pending_reopen_issue_ids=pending_ids) for issue in issues
    ]


def list_admin_issues(
    db: Session,
    *,
    status: IssueStatus | None = None,
    department_code: str | None = None,
    severity: SeverityLevel | None = None,
    category: IssueCategory | None = None,
    jurisdiction_code: str | None = None,
    sort_by: str = "updated_at",
    sort_order: str = "desc",
    limit: int = 20,
    offset: int = 0,
) -> AdminIssueListResponse | None:
    """Build the paginated, filtered authority issue list. Never calls
    Gemini -- pure read over already-persisted issues. Returns None if
    jurisdiction_code doesn't match any real jurisdiction (route -> 404)."""
    result = list_issues_for_admin(
        db,
        status=status,
        department_code=department_code,
        severity=severity,
        category=category,
        jurisdiction_code=jurisdiction_code,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
    )
    if result is None:
        return None
    items, total = result
    return AdminIssueListResponse(
        items=_build_admin_list_items(db, items),
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
    evidence = [
        EvidenceResponse(
            public_id=e.public_id,
            original_filename=e.original_filename,
            content_type=e.content_type,
            size_bytes=e.size_bytes,
            uploaded_by=e.uploaded_by,
            uploaded_at=e.uploaded_at,
        )
        for e in issue.evidence
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
        resolved_by=issue.resolved_by,
        latitude=issue.latitude,
        longitude=issue.longitude,
        location_accuracy=issue.location_accuracy,
        citizen_language=issue.citizen_language,
        created_at=issue.created_at,
        updated_at=issue.updated_at,
        resolved_at=issue.resolved_at,
        closed_at=issue.closed_at,
        status_history=status_history,
        assignment_history=assignment_history,
        evidence=evidence,
        pending_reopen_request=get_issue_pending_reopen_request(db, issue),
    )


def get_dashboard_summary(
    db: Session, *, jurisdiction_code: str | None = None
) -> DashboardSummaryResponse | None:
    """Build the dashboard aggregate-count summary. Every count comes from
    a SQL GROUP BY query (see backend.repository) -- this function never
    iterates raw Issue rows. jurisdiction_code scopes by_status/by_severity
    (and total_issues) to that jurisdiction's subtree; by_department is
    intentionally always the full active-department list regardless of
    scoping, since it already shows each department's own count (0 for
    departments outside the scope reads the same as "no issues", so no
    information is misrepresented). Returns None if jurisdiction_code
    doesn't match any real jurisdiction (route -> 404). Never calls
    Gemini.

    active_issue_count and active_high_critical_count are the single
    server-computed source of truth for the Command Center's "Active" and
    "High/Critical Active" KPI cards -- both use METRIC_INACTIVE_STATUSES
    (backend.repository), which correctly excludes RESOLVED issues from
    "active", not just CLOSED/REJECTED. The frontend must display these
    values as-is rather than re-deriving them from by_status/by_severity,
    since by_severity alone has no status information to filter on."""
    by_status = count_issues_by_status(db, jurisdiction_code=jurisdiction_code)
    if by_status is None:
        return None
    by_severity = count_issues_by_severity(db, jurisdiction_code=jurisdiction_code)
    by_department = [
        DepartmentIssueCount(code=department.code, name=department.name, count=count)
        for department, count in count_issues_by_department(db)
    ]
    active_issue_count = sum(
        count for status_value, count in by_status.items() if status_value not in METRIC_INACTIVE_STATUSES
    )
    high_critical_counts = count_active_high_severity_issues(db, jurisdiction_code=jurisdiction_code)
    active_high_critical_count = sum(high_critical_counts.values()) if high_critical_counts else 0
    return DashboardSummaryResponse(
        total_issues=sum(by_status.values()),
        by_status=by_status,
        by_severity=by_severity,
        by_department=by_department,
        active_issue_count=active_issue_count,
        active_high_critical_count=active_high_critical_count,
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
    jurisdiction_code: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> AdminIssueListResponse | None:
    """Build the operational queue (excludes CLOSED/REJECTED, ordered by
    severity priority then oldest updated_at). Returns None if
    jurisdiction_code doesn't match any real jurisdiction. Never calls
    Gemini."""
    result = repo_get_operational_queue(
        db,
        department_code=department_code,
        severity=severity,
        jurisdiction_code=jurisdiction_code,
        limit=limit,
        offset=offset,
    )
    if result is None:
        return None
    items, total = result
    return AdminIssueListResponse(
        items=_build_admin_list_items(db, items),
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
    jurisdiction_code: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> AdminIssueListResponse | None:
    """Build the stale/aging-issues list. Read-only. Returns None if
    jurisdiction_code doesn't match any real jurisdiction. Never calls
    Gemini."""
    result = repo_get_stale_issues(
        db,
        older_than_hours=older_than_hours,
        department_code=department_code,
        status=status,
        jurisdiction_code=jurisdiction_code,
        limit=limit,
        offset=offset,
    )
    if result is None:
        return None
    items, total = result
    return AdminIssueListResponse(
        items=_build_admin_list_items(db, items),
        total=total,
        limit=limit,
        offset=offset,
    )


# --- Civic Intelligence & Analytics (Milestone 9) ----------------------------


def get_status_funnel(
    db: Session, *, jurisdiction_code: str | None = None
) -> FunnelResponse | None:
    """Dashboard-ready status funnel. Reuses count_issues_by_status()
    (Milestone 8) rather than issuing a duplicate GROUP BY query -- only
    the response shape differs (an ordered list for a funnel chart,
    instead of a dict). jurisdiction_code scopes via Issue.jurisdiction_id
    (Milestone 17); returns None if the code doesn't match any real
    jurisdiction (route -> 404). Never calls Gemini."""
    by_status = count_issues_by_status(db, jurisdiction_code=jurisdiction_code)
    if by_status is None:
        return None
    stages = [
        FunnelStage(status=status_value, status_label=_status_label(status_value), count=count)
        for status_value, count in by_status.items()
    ]
    return FunnelResponse(stages=stages, total_issues=sum(by_status.values()))


def get_severity_distribution(
    db: Session, *, jurisdiction_code: str | None = None
) -> SeverityDistributionResponse | None:
    """Severity breakdown with percentages. Reuses
    count_issues_by_severity() (Milestone 8) -- no duplicate query. Uses
    the existing SeverityLevel enum only. jurisdiction_code scopes via
    Issue.jurisdiction_id (Milestone 17); returns None if the code
    doesn't match any real jurisdiction (route -> 404). Never calls
    Gemini."""
    by_severity = count_issues_by_severity(db, jurisdiction_code=jurisdiction_code)
    if by_severity is None:
        return None
    total = sum(by_severity.values())
    distribution = [
        SeverityDistributionEntry(
            severity=severity_value,
            count=count,
            percentage=round((count / total * 100) if total else 0.0, 2),
        )
        for severity_value, count in by_severity.items()
    ]
    return SeverityDistributionResponse(distribution=distribution, total_issues=total)


def get_department_workload_summary(
    db: Session, *, jurisdiction_code: str | None = None
) -> DepartmentWorkloadResponse | None:
    """Workload (status breakdown + active/resolved/closed rollup) for
    every active department in one call. Based on the OFFICIAL assigned
    department only, never suggested_department. jurisdiction_code scopes
    to departments belonging to that jurisdiction (Milestone 17); returns
    None if the code doesn't match any real jurisdiction (route -> 404).
    Never calls Gemini."""
    workload = get_department_workload(db, jurisdiction_code=jurisdiction_code)
    if workload is None:
        return None
    departments = [
        DepartmentWorkloadEntry(
            code=department.code,
            name=department.name,
            total_assigned=sum(by_status.values()),
            active_issues=sum(
                count for st, count in by_status.items() if st not in METRIC_INACTIVE_STATUSES
            ),
            resolved_issues=by_status[IssueStatus.RESOLVED],
            closed_issues=by_status[IssueStatus.CLOSED],
            by_status=by_status,
        )
        for department, by_status in workload
    ]
    return DepartmentWorkloadResponse(departments=departments)


def get_aging_buckets(
    db: Session, *, jurisdiction_code: str | None = None
) -> AgingBucketsResponse | None:
    """Age-since-submission distribution for currently active issues.
    Neutral duration buckets, not SLA targets. jurisdiction_code scopes
    via Issue.jurisdiction_id (Milestone 17); returns None if the code
    doesn't match any real jurisdiction (route -> 404). Never calls
    Gemini."""
    buckets = get_issue_aging_buckets(db, jurisdiction_code=jurisdiction_code)
    if buckets is None:
        return None
    return AgingBucketsResponse(
        buckets=[AgingBucketEntry(bucket=label, count=count) for label, count in buckets.items()],
        total_active_issues=sum(buckets.values()),
    )


def get_resolution_timing(
    db: Session, *, jurisdiction_code: str | None = None
) -> ResolutionTimingResponse | None:
    """Resolution/closure timing metrics. jurisdiction_code scopes via
    Issue.jurisdiction_id (Milestone 17); returns None if the code
    doesn't match any real jurisdiction (route -> 404). Never calls
    Gemini."""
    metrics = get_resolution_timing_metrics(db, jurisdiction_code=jurisdiction_code)
    if metrics is None:
        return None
    return ResolutionTimingResponse(**metrics)


def get_resolution_intelligence(
    db: Session, *, jurisdiction_code: str | None = None
) -> ResolutionIntelligenceResponse | None:
    """Resolution, reopening, and evidence-coverage KPIs (Milestone 19).
    jurisdiction_code scopes via Issue.jurisdiction_id, the same
    convention as every other jurisdiction-aware analytics function in
    this module; returns None if the code doesn't match any real
    jurisdiction (route -> 404). Purely deterministic -- never calls
    Gemini, and never mutates any Issue, ReopenRequest, or history row.
    """
    raw = repo_get_resolution_intelligence(db, jurisdiction_code=jurisdiction_code)
    if raw is None:
        return None

    total_resolved = raw["total_resolved"]
    resolution_rate_percent = (
        round(100 * total_resolved / raw["total_non_rejected_issues"], 1)
        if raw["total_non_rejected_issues"] > 0
        else None
    )
    reopen_rate_percent = (
        round(100 * raw["approved_reopen_requests"] / total_resolved, 1)
        if total_resolved > 0
        else None
    )
    evidence_coverage_percent = (
        round(100 * raw["resolutions_with_evidence"] / total_resolved, 1)
        if total_resolved > 0
        else None
    )

    return ResolutionIntelligenceResponse(
        total_issues=raw["total_issues"],
        total_resolved=total_resolved,
        resolution_rate_percent=resolution_rate_percent,
        avg_resolution_hours=(
            round(raw["avg_resolution_hours"], 1) if raw["avg_resolution_hours"] is not None else None
        ),
        median_resolution_hours=(
            round(raw["median_resolution_hours"], 1)
            if raw["median_resolution_hours"] is not None
            else None
        ),
        currently_reopened=raw["currently_reopened"],
        total_reopen_requests=raw["total_reopen_requests"],
        pending_reopen_requests=raw["pending_reopen_requests"],
        approved_reopen_requests=raw["approved_reopen_requests"],
        rejected_reopen_requests=raw["rejected_reopen_requests"],
        reopen_rate_percent=reopen_rate_percent,
        resolutions_with_evidence=raw["resolutions_with_evidence"],
        resolutions_without_evidence=raw["resolutions_without_evidence"],
        evidence_coverage_percent=evidence_coverage_percent,
        approved_reopens_by_category=[
            CategoryReopenCount(category=category, count=count)
            for category, count in raw["approved_reopens_by_category"].items()
        ],
    )


def get_geo_issues(
    db: Session, *, jurisdiction_code: str | None = None
) -> list[GeoIssueSummary] | None:
    """Geo-tagged issues for the authority-facing map/list view
    (Milestone 20). Returns None if jurisdiction_code doesn't match any
    real jurisdiction (route -> 404)."""
    issues = repo_get_geospatial_issues(db, jurisdiction_code=jurisdiction_code)
    if issues is None:
        return None
    return [
        GeoIssueSummary(
            public_id=issue.public_id,
            latitude=issue.latitude,
            longitude=issue.longitude,
            category=issue.category,
            severity=issue.severity,
            status=issue.status,
            status_label=_status_label(issue.status),
            created_at=issue.created_at,
        )
        for issue in issues
    ]


def get_civic_hotspots(
    db: Session, *, jurisdiction_code: str | None = None
) -> HotspotsResponse | None:
    """Detect civic hotspots (Milestone 20) -- deterministic geographic
    clustering (backend/hotspots.py) over active, geo-tagged issues in
    scope. Never calls Gemini, never mutates any Issue. Returns None if
    jurisdiction_code doesn't match any real jurisdiction (route -> 404).
    """
    issues = repo_get_geospatial_issues(db, jurisdiction_code=jurisdiction_code)
    if issues is None:
        return None

    points = [
        GeoIssuePoint(
            public_id=issue.public_id,
            latitude=issue.latitude,
            longitude=issue.longitude,
            category=issue.category.value,
            severity=issue.severity.value,
            status=_status_label(issue.status),
            created_at=issue.created_at,
        )
        for issue in issues
    ]
    detected = detect_hotspots(points)

    return HotspotsResponse(
        hotspots=[
            HotspotResponse(
                center_latitude=round(h.center_latitude, 4),
                center_longitude=round(h.center_longitude, 4),
                complaint_count=h.complaint_count,
                dominant_category=h.dominant_category,
                category_breakdown=h.category_breakdown,
                severity_breakdown=h.severity_breakdown,
                status_breakdown=h.status_breakdown,
                earliest_complaint_at=h.earliest_complaint_at,
                latest_complaint_at=h.latest_complaint_at,
                priority_signal=h.priority_signal,
                member_public_ids=h.member_public_ids,
            )
            for h in detected
        ],
        geo_tagged_issue_count=len(points),
        generated_at=datetime.now(timezone.utc),
    )


def get_recent_activity_feed(
    db: Session, limit: int = 20, *, jurisdiction_code: str | None = None
) -> RecentActivityResponse | None:
    """Most recent status-transition events, newest first.
    jurisdiction_code scopes via Issue.jurisdiction_id (Milestone 17);
    returns None if the code doesn't match any real jurisdiction (route
    -> 404). Never calls Gemini."""
    activity = get_recent_activity(db, limit=limit, jurisdiction_code=jurisdiction_code)
    if activity is None:
        return None
    activities = [
        RecentActivityEntry(
            public_id=issue.public_id,
            category=issue.category,
            from_status=history_entry.from_status,
            to_status=history_entry.to_status,
            reason=history_entry.reason,
            changed_at=history_entry.changed_at,
        )
        for history_entry, issue in activity
    ]
    return RecentActivityResponse(activities=activities)


# --- Civic Accountability & Intelligence (Milestone 10) ----------------------


def get_civic_insights(
    db: Session, *, jurisdiction_code: str | None = None
) -> InsightsResponse | None:
    """Compute every grounded, deterministic civic insight.

    Purely deterministic -- never calls Gemini. Reuses existing Milestone
    8/9 aggregate queries (count_issues_by_status, get_department_workload,
    get_stale_issues) rather than re-querying the same data, plus two new
    small aggregate queries (count_active_high_severity_issues,
    count_active_issues_by_category) for signals M8/M9 didn't already
    compute. The actual "does this data support an insight" decisions live
    in backend/insights.py, kept separate and DB-free so those rules are
    unit-testable without a database.

    jurisdiction_code (Milestone 17) scopes every underlying query to that
    jurisdiction's subtree, so insights are computed only from issues
    inside the current jurisdiction -- returns None if the code doesn't
    match any real jurisdiction (route -> 404).
    """
    insights: list[Insight] = []

    high_severity_counts = count_active_high_severity_issues(db, jurisdiction_code=jurisdiction_code)
    if high_severity_counts is None:
        return None
    high_severity_insight = insight_rules.build_high_severity_insight(high_severity_counts)
    if high_severity_insight is not None:
        insights.append(high_severity_insight)

    workload = get_department_workload(db, jurisdiction_code=jurisdiction_code)
    if workload is None:
        return None
    department_active_counts = [
        (
            department.code,
            department.name,
            sum(count for st, count in by_status.items() if st not in METRIC_INACTIVE_STATUSES),
        )
        for department, by_status in workload
    ]
    insights.extend(insight_rules.build_department_concentration_insights(department_active_counts))

    # Reuse the existing stale-issues query (Milestone 8) purely for its
    # `total` -- limit=1 avoids fetching the full item list we don't need
    # here.
    stale_result = repo_get_stale_issues(
        db, older_than_hours=48, limit=1, offset=0, jurisdiction_code=jurisdiction_code
    )
    if stale_result is None:
        return None
    _stale_items, stale_total = stale_result
    stale_insight = insight_rules.build_stale_concentration_insight(
        stale_total, older_than_hours=48
    )
    if stale_insight is not None:
        insights.append(stale_insight)

    category_result = count_active_issues_by_category(db, jurisdiction_code=jurisdiction_code)
    if category_result is None:
        return None
    category_counts = {category.value: count for category, count in category_result.items()}
    recurring_insight = insight_rules.build_recurring_category_insight(category_counts)
    if recurring_insight is not None:
        insights.append(recurring_insight)

    status_counts = count_issues_by_status(db, jurisdiction_code=jurisdiction_code)
    if status_counts is None:
        return None
    bottleneck_insight = insight_rules.build_lifecycle_bottleneck_insight(
        status_counts, TERMINAL_STATUSES
    )
    if bottleneck_insight is not None:
        insights.append(bottleneck_insight)

    # Milestone 19: reuses status_counts fetched immediately above --
    # no duplicate query needed.
    reopened_insight = insight_rules.build_reopened_attention_insight(
        status_counts[IssueStatus.REOPENED]
    )
    if reopened_insight is not None:
        insights.append(reopened_insight)

    # Milestone 20: deterministic hotspot detection, wrapped as insights.
    # Capped at the top 3 by complaint count to avoid insight-list bloat
    # -- detect_hotspots() already returns them in that order.
    hotspots_result = get_civic_hotspots(db, jurisdiction_code=jurisdiction_code)
    if hotspots_result is None:
        return None
    for hotspot in hotspots_result.hotspots[:3]:
        insights.append(insight_rules.build_hotspot_insight(hotspot))

    return InsightsResponse(insights=insights, generated_at=datetime.now(timezone.utc))


def get_prioritized_insights(
    db: Session, *, jurisdiction_code: str | None = None
) -> PrioritizedInsightsResponse | None:
    """Compute grounded insights, then ask Gemini for an advisory priority
    order and explanation over them.

    ADVISORY ONLY: nothing here ever changes an Issue's status,
    assignment, or any official record -- see
    ai/prompts.py's PRIORITIZATION_SYSTEM_PROMPT for the enforced
    boundary. Only aggregate, non-identifying insight fields are sent to
    Gemini (never original_text, public_id, or confidence).

    jurisdiction_code (Milestone 17) scopes the underlying insights the
    same way GET /api/admin/insights does; returns None if the code
    doesn't match any real jurisdiction (route -> 404).

    If there are no insights, Gemini is never called (nothing to
    prioritize, and calling it anyway would risk fabricating a
    recommendation with no grounding). If Gemini is unavailable, fails, or
    returns something unusable, the grounded insights are still returned
    with ai_recommendation=None and a sanitized ai_recommendation_error --
    their value doesn't depend on Gemini succeeding.
    """
    insights_response = get_civic_insights(db, jurisdiction_code=jurisdiction_code)
    if insights_response is None:
        return None
    if not insights_response.insights:
        return PrioritizedInsightsResponse(
            insights=[], ai_recommendation=None, ai_recommendation_error=None
        )

    payload = [
        {
            "insight_type": insight.insight_type,
            "priority": insight.priority.value,
            "title": insight.title,
            "summary": insight.summary,
            "affected_issue_count": insight.affected_issue_count,
            "affected_department": (
                insight.affected_department.code if insight.affected_department else None
            ),
            "evidence": insight.evidence,
        }
        for insight in insights_response.insights
    ]
    known_types = {insight.insight_type for insight in insights_response.insights}

    try:
        ai_output = generate_insight_prioritization(payload)
    except (EnvironmentError, APIError, ValueError):
        return PrioritizedInsightsResponse(
            insights=insights_response.insights,
            ai_recommendation=None,
            ai_recommendation_error="AI prioritization is temporarily unavailable.",
        )

    # Sanitize against hallucinated/incomplete output: keep only
    # insight_types we actually sent (in Gemini's order), then append any
    # we sent that Gemini omitted. This never lets Gemini add, rename, or
    # drop an insight -- it can only reorder what CivicSync computed.
    sanitized_order = [t for t in ai_output.recommended_priority_order if t in known_types]
    for insight_type in known_types:
        if insight_type not in sanitized_order:
            sanitized_order.append(insight_type)

    return PrioritizedInsightsResponse(
        insights=insights_response.insights,
        ai_recommendation=AIPrioritizationRecommendation(
            summary=ai_output.summary,
            recommended_priority_order=sanitized_order,
            explanation=ai_output.explanation,
        ),
        ai_recommendation_error=None,
    )


# --- Jurisdiction hierarchy (Milestone 13) -----------------------------------


def _to_jurisdiction_response(
    jurisdiction, ancestry: list
) -> JurisdictionResponse:
    return JurisdictionResponse(
        code=jurisdiction.code,
        name=jurisdiction.name,
        level=jurisdiction.level,
        country_code=jurisdiction.country_code,
        parent_code=jurisdiction.parent.code if jurisdiction.parent else None,
        is_active=jurisdiction.is_active,
        ancestry=[
            JurisdictionSummary(code=j.code, name=j.name, level=j.level) for j in ancestry
        ],
    )


def get_jurisdictions(
    db: Session, *, level: JurisdictionLevel | None = None, country_code: str | None = None
) -> JurisdictionListResponse:
    """List active jurisdictions, each with its full ancestry chain
    already attached (no second request per row to build a breadcrumb).
    Exactly two queries total regardless of how many jurisdictions are
    returned (see get_jurisdictions_with_ancestry). Never calls Gemini.
    """
    pairs = get_jurisdictions_with_ancestry(db, level=level, country_code=country_code)
    return JurisdictionListResponse(
        jurisdictions=[_to_jurisdiction_response(j, ancestry) for j, ancestry in pairs]
    )


def get_jurisdiction_detail(db: Session, code: str) -> JurisdictionResponse | None:
    """Single jurisdiction by code, with its ancestry chain. Returns None
    if no jurisdiction matches (route -> 404). Never calls Gemini."""
    jurisdiction = get_jurisdiction_by_code(db, code)
    if jurisdiction is None:
        return None
    ancestry = get_jurisdiction_ancestry(db, jurisdiction)
    return _to_jurisdiction_response(jurisdiction, ancestry)


# --- On-demand AI issue explanation (Milestone 14, Priority 2) ---------------


def get_issue_ai_explanation(db: Session, public_id: str) -> IssueExplanationResponse | None:
    """Ask Gemini to explain a single issue's existing classification, on
    demand, for an authority reviewing it. Returns None if no Issue
    matches public_id (route -> 404).

    Never persisted: reads the issue's already-extracted fields, calls
    Gemini fresh, and returns the result without writing anything back.
    Calling this twice may produce different explanations -- expected,
    since nothing here is authoritative.

    Only structured/extracted fields are sent to Gemini (category,
    problem, location, duration, affected_population, severity,
    confidence, suggested_department, assigned_department, status_label)
    -- never original_text or public_id, matching get_prioritized_insights().

    Advisory only: Gemini explains why the existing classification/
    department make sense and is instructed never to propose changing
    them (see EXPLANATION_SYSTEM_PROMPT). This function adds a code-level
    guarantee on top of that -- no path here writes to Issue,
    IssueStatusHistory, or IssueAssignmentHistory.

    If Gemini is unavailable or fails, a sanitized error is returned
    instead of fabricating an explanation -- never blocks the rest of
    the authority workflow.
    """
    issue = get_issue_by_public_id(db, public_id)
    if issue is None:
        return None

    issue_context = {
        "category": issue.category,
        "problem": issue.problem,
        "location": issue.location,
        "duration": issue.duration,
        "affected_population": issue.affected_population,
        "severity": issue.severity.value,
        "confidence": issue.confidence,
        "suggested_department": issue.suggested_department,
        "assigned_department": issue.assigned_department.name if issue.assigned_department else None,
        "status_label": _status_label(issue.status),
    }

    try:
        ai_output = generate_issue_explanation(issue_context)
    except (EnvironmentError, APIError, ValueError):
        return IssueExplanationResponse(
            explanation=None,
            considerations=[],
            error="AI explanation is temporarily unavailable.",
        )

    return IssueExplanationResponse(
        explanation=ai_output.explanation,
        considerations=ai_output.considerations,
        error=None,
    )


# --- AI Operational Briefing (Milestone 15) -----------------------------------


def get_operational_briefing(
    db: Session, *, jurisdiction_code: str, department_code: str | None = None
) -> OperationalBriefingResponse | None:
    """Ask Gemini to synthesize a short operational briefing for a
    jurisdiction (and optionally a single department), on demand, for an
    authority reviewing the current situation. Returns None if
    jurisdiction_code doesn't match any real jurisdiction (route -> 404).

    NEVER PERSISTED: this reads the currently active issues in scope
    (already-extracted structured fields only), calls Gemini fresh, and
    returns the result without writing anything back to any table -- no
    new column, no migration, nothing stored. Calling this twice in a row
    can produce two different (though similarly-grounded) briefings;
    that's expected and fine, since nothing here is authoritative.

    Reuses get_active_issues_for_briefing for jurisdiction+department
    scoping (no jurisdiction logic duplicated here). Only structured,
    per-issue fields are sent to Gemini (category, severity, status
    label, department name) -- never original_text, public_id, or any
    citizen-identifying data, matching the same privacy discipline as
    get_prioritized_insights() and get_issue_ai_explanation() above.

    If there are no active issues in scope, Gemini is never called
    (nothing to brief on, and calling it anyway would risk fabricating a
    pattern with no grounding) -- a plain, honest, non-AI-generated
    message is returned via `error` instead. If Gemini is unavailable,
    fails, or returns something unusable, a sanitized (never
    raw-exception) `error` is returned instead of fabricating a
    briefing -- this never blocks the rest of the authority workflow.
    """
    issues = get_active_issues_for_briefing(
        db, jurisdiction_code=jurisdiction_code, department_code=department_code
    )
    if issues is None:
        return None

    if not issues:
        return OperationalBriefingResponse(
            briefing=None,
            key_observations=[],
            priority_signals=[],
            considerations=[],
            error="No active issues in this scope to generate a briefing from.",
        )

    payload = {
        "jurisdiction_code": jurisdiction_code,
        "department_code": department_code,
        "total_active_issues": len(issues),
        "issues": [
            {
                "category": issue.category,
                "severity": issue.severity.value,
                "status": _status_label(issue.status),
                "department": issue.assigned_department.name if issue.assigned_department else None,
            }
            for issue in issues
        ],
    }

    try:
        ai_output = generate_operational_briefing(payload)
    except (EnvironmentError, APIError, ValueError):
        return OperationalBriefingResponse(
            briefing=None,
            key_observations=[],
            priority_signals=[],
            considerations=[],
            error="AI operational briefing is temporarily unavailable.",
        )

    return OperationalBriefingResponse(
        briefing=ai_output.briefing,
        key_observations=ai_output.key_observations,
        priority_signals=ai_output.priority_signals,
        considerations=ai_output.considerations,
        error=None,
    )


# --- Resolution evidence (Milestone 18, Phase 3) -----------------------------

# Maps an accepted Content-Type to its stored file extension and the exact
# PIL format name Pillow reports for a genuine file of that type. Both the
# declared Content-Type AND the actual decoded image format must match
# something in this table -- neither is trusted alone. Limited to the
# formats explicitly requested; nothing here accepts an extension just
# because a filename claims it.
_ALLOWED_EVIDENCE_TYPES: dict[str, tuple[str, str]] = {
    "image/jpeg": (".jpg", "JPEG"),
    "image/png": (".png", "PNG"),
    "image/webp": (".webp", "WEBP"),
}

def _max_evidence_size_bytes() -> int:
    """Read fresh on every call (not cached at import time) so
    CIVICSYNC_EVIDENCE_MAX_SIZE_BYTES is genuinely configurable per
    request/process, not frozen at whatever value happened to be set
    when this module was first imported."""
    return int(os.environ.get("CIVICSYNC_EVIDENCE_MAX_SIZE_BYTES", str(5 * 1024 * 1024)))


class UnsupportedEvidenceTypeError(Exception):
    """Raised when uploaded evidence isn't a genuine, supported image --
    covers a rejected Content-Type, a Content-Type/actual-bytes mismatch,
    an empty file, and corrupt/unparseable image data. The route maps
    this to 400."""


class EvidenceTooLargeError(Exception):
    """Raised when uploaded evidence exceeds _MAX_EVIDENCE_SIZE_BYTES.
    The route maps this to 413."""


def _sanitize_evidence_display_filename(filename: str) -> str:
    """A safe DISPLAY name only -- this value is never used to construct
    a filesystem path or to look up a stored file (storage_key, a
    server-generated random value, is what's actually used for that).
    Strips any path component the client might have sent (defense in
    depth against a filename like "../../etc/passwd") and any character
    outside a conservative safe set.
    """
    base = os.path.basename((filename or "").strip()) or "evidence"
    safe = "".join(ch for ch in base if ch.isalnum() or ch in "._- ")
    safe = safe.strip() or "evidence"
    return safe[:200]


def _verify_genuine_image(content: bytes, declared_content_type: str) -> None:
    """Confirm `content` is actually a valid, decodable image whose real
    format matches declared_content_type -- never trusts the
    Content-Type header (easily spoofed) or a filename extension alone.
    Raises UnsupportedEvidenceTypeError if anything doesn't check out.
    """
    expected = _ALLOWED_EVIDENCE_TYPES.get(declared_content_type)
    if expected is None:
        raise UnsupportedEvidenceTypeError(
            f"Unsupported content type: {declared_content_type!r}."
        )
    _extension, expected_format = expected

    if not content:
        raise UnsupportedEvidenceTypeError("Uploaded file is empty.")

    try:
        # A fast structural check first...
        with Image.open(BytesIO(content)) as probe:
            probe.verify()
        # ...then a fresh, fully-decoding open (verify() leaves the
        # image object unusable for further reads) to confirm the pixel
        # data itself is genuinely decodable, not just a valid header.
        with Image.open(BytesIO(content)) as image:
            detected_format = image.format
            image.load()
    except (UnidentifiedImageError, OSError, ValueError):
        raise UnsupportedEvidenceTypeError("File is not a valid image.") from None

    if detected_format != expected_format:
        raise UnsupportedEvidenceTypeError(
            "The file's actual content does not match its declared content type."
        )


def upload_issue_evidence(
    db: Session,
    public_id: str,
    *,
    filename: str,
    content_type: str,
    content: bytes,
    uploaded_by: str,
) -> EvidenceResponse | None:
    """Validate and store one resolution evidence file for an issue.

    Returns None if no Issue matches public_id (route -> 404). Raises
    UnsupportedEvidenceTypeError (route -> 400) or EvidenceTooLargeError
    (route -> 413) if validation fails -- in either case, NOTHING is
    written to storage or the database. `uploaded_by` must be the
    server-side authenticated authority identity, never accepted from
    request input.

    Does not require the issue to be in any particular status -- an
    authority may attach evidence at any point while working an issue,
    not only at the exact moment of calling POST .../resolve (Milestone
    18, Phase 2), which remains completely unchanged by this function.

    Storage ordering for safety: the file is written to
    backend.evidence_storage BEFORE the database row is created. If the
    database write then fails, the orphaned file is best-effort cleaned
    up before the error propagates -- this avoids ever creating a
    database row that points at a file that was never actually written,
    the more dangerous failure mode for later retrieval.
    """
    issue = get_issue_by_public_id(db, public_id)
    if issue is None:
        return None

    max_size = _max_evidence_size_bytes()
    if len(content) > max_size:
        raise EvidenceTooLargeError(
            f"Evidence file exceeds the maximum allowed size of {max_size} bytes."
        )

    _verify_genuine_image(content, content_type)

    extension, _expected_format = _ALLOWED_EVIDENCE_TYPES[content_type]
    # Server-generated, unguessable, and never derived from client input
    # -- this is the ONLY value ever passed to evidence_storage, which is
    # what makes path traversal via a crafted filename impossible.
    storage_key = f"{secrets.token_hex(16)}{extension}"

    evidence_storage.save(storage_key, content)
    try:
        evidence = create_evidence(
            db,
            issue,
            storage_key=storage_key,
            original_filename=_sanitize_evidence_display_filename(filename),
            content_type=content_type,
            size_bytes=len(content),
            uploaded_by=uploaded_by,
        )
    except SQLAlchemyError:
        db.rollback()
        raise

    return EvidenceResponse(
        public_id=evidence.public_id,
        original_filename=evidence.original_filename,
        content_type=evidence.content_type,
        size_bytes=evidence.size_bytes,
        uploaded_by=evidence.uploaded_by,
        uploaded_at=evidence.uploaded_at,
    )


def get_issue_evidence(db: Session, public_id: str) -> list[EvidenceResponse] | None:
    """List evidence metadata for an issue. Returns None if no Issue
    matches public_id (route -> 404)."""
    issue = get_issue_by_public_id(db, public_id)
    if issue is None:
        return None
    return [
        EvidenceResponse(
            public_id=e.public_id,
            original_filename=e.original_filename,
            content_type=e.content_type,
            size_bytes=e.size_bytes,
            uploaded_by=e.uploaded_by,
            uploaded_at=e.uploaded_at,
        )
        for e in get_evidence_for_issue(db, issue)
    ]


def get_evidence_file(db: Session, evidence_public_id: str) -> tuple[bytes, str, str] | None:
    """Retrieve one evidence file's bytes for download/display.

    Returns (content_bytes, content_type, display_filename), or None if
    no evidence matches evidence_public_id (route -> 404). Looks up the
    file exclusively via the database row's own storage_key -- a caller
    can never supply or influence a filesystem path directly, which is
    what prevents path traversal here.
    """
    evidence = get_evidence_by_public_id(db, evidence_public_id)
    if evidence is None:
        return None
    content = evidence_storage.load(evidence.storage_key)
    return content, evidence.content_type, evidence.original_filename


def get_public_evidence_file(
    db: Session, issue_public_id: str, evidence_public_id: str
) -> tuple[bytes, str, str] | None:
    """Citizen-facing evidence retrieval (Milestone 18, Phase 4) --
    narrowly scoped to the existing public tracking model: a valid
    evidence_public_id is NOT enough on its own. This also verifies that
    evidence actually belongs to the issue identified by issue_public_id
    (the same public_id the citizen is already tracking) before
    returning anything. Without this check, a citizen who somehow
    obtained or guessed a DIFFERENT issue's evidence_public_id could
    retrieve evidence belonging to a report that isn't theirs -- exactly
    the cross-issue access this function exists to prevent.

    Returns None (route -> 404) if the issue doesn't exist, the evidence
    doesn't exist, OR the evidence belongs to a different issue -- all
    three cases are indistinguishable in the response, so this can't be
    used to probe for the existence of evidence on an issue the caller
    doesn't already know the public_id of.
    """
    issue = get_issue_by_public_id(db, issue_public_id)
    if issue is None:
        return None
    evidence = get_evidence_by_public_id(db, evidence_public_id)
    if evidence is None or evidence.issue_id != issue.id:
        return None
    content = evidence_storage.load(evidence.storage_key)
    return content, evidence.content_type, evidence.original_filename
