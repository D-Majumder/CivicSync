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

from datetime import datetime, timezone

from google.genai.errors import APIError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ai.client import analyze_complaint, generate_insight_prioritization, generate_issue_explanation
from ai.schemas import IssueCategory, SeverityLevel
from backend import insights as insight_rules
from backend.assignment_rules import DepartmentInactiveError, DepartmentNotFoundError
from backend.models import Department, Issue, IssueStatus, JurisdictionLevel
from backend.repository import (
    TERMINAL_STATUSES,
    assign_department_to_issue,
    count_active_high_severity_issues,
    count_active_issues_by_category,
    count_issues_by_department,
    count_issues_by_severity,
    count_issues_by_status,
    create_issue_from_civic_issue,
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
    get_resolution_timing_metrics,
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
    AgingBucketEntry,
    AgingBucketsResponse,
    AIPrioritizationRecommendation,
    DashboardSummaryResponse,
    DepartmentIssueCount,
    DepartmentSummary,
    DepartmentSummaryResponse,
    DepartmentWorkloadEntry,
    DepartmentWorkloadResponse,
    FunnelResponse,
    FunnelStage,
    Insight,
    InsightsResponse,
    IssueExplanationResponse,
    JurisdictionListResponse,
    JurisdictionResponse,
    JurisdictionSummary,
    PrioritizedInsightsResponse,
    PublicIssueTrackingResponse,
    PublicTimelineEntry,
    RecentActivityEntry,
    RecentActivityResponse,
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
    Gemini."""
    by_status = count_issues_by_status(db, jurisdiction_code=jurisdiction_code)
    if by_status is None:
        return None
    by_severity = count_issues_by_severity(db, jurisdiction_code=jurisdiction_code)
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
        items=[_build_admin_list_item(issue) for issue in items],
        total=total,
        limit=limit,
        offset=offset,
    )


# --- Civic Intelligence & Analytics (Milestone 9) ----------------------------


def get_status_funnel(db: Session) -> FunnelResponse:
    """Dashboard-ready status funnel. Reuses count_issues_by_status()
    (Milestone 8) rather than issuing a duplicate GROUP BY query -- only
    the response shape differs (an ordered list for a funnel chart,
    instead of a dict). Never calls Gemini."""
    by_status = count_issues_by_status(db)
    stages = [
        FunnelStage(status=status_value, status_label=_status_label(status_value), count=count)
        for status_value, count in by_status.items()
    ]
    return FunnelResponse(stages=stages, total_issues=sum(by_status.values()))


def get_severity_distribution(db: Session) -> SeverityDistributionResponse:
    """Severity breakdown with percentages. Reuses
    count_issues_by_severity() (Milestone 8) -- no duplicate query. Uses
    the existing SeverityLevel enum only. Never calls Gemini."""
    by_severity = count_issues_by_severity(db)
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


def get_department_workload_summary(db: Session) -> DepartmentWorkloadResponse:
    """Workload (status breakdown + active/resolved/closed rollup) for
    every active department in one call. Based on the OFFICIAL assigned
    department only, never suggested_department. Never calls Gemini."""
    departments = [
        DepartmentWorkloadEntry(
            code=department.code,
            name=department.name,
            total_assigned=sum(by_status.values()),
            active_issues=sum(
                count for st, count in by_status.items() if st not in TERMINAL_STATUSES
            ),
            resolved_issues=by_status[IssueStatus.RESOLVED],
            closed_issues=by_status[IssueStatus.CLOSED],
            by_status=by_status,
        )
        for department, by_status in get_department_workload(db)
    ]
    return DepartmentWorkloadResponse(departments=departments)


def get_aging_buckets(db: Session) -> AgingBucketsResponse:
    """Age-since-submission distribution for currently active issues.
    Neutral duration buckets, not SLA targets. Never calls Gemini."""
    buckets = get_issue_aging_buckets(db)
    return AgingBucketsResponse(
        buckets=[AgingBucketEntry(bucket=label, count=count) for label, count in buckets.items()],
        total_active_issues=sum(buckets.values()),
    )


def get_resolution_timing(db: Session) -> ResolutionTimingResponse:
    """Resolution/closure timing metrics. Never calls Gemini."""
    metrics = get_resolution_timing_metrics(db)
    return ResolutionTimingResponse(**metrics)


def get_recent_activity_feed(db: Session, limit: int = 20) -> RecentActivityResponse:
    """Most recent status-transition events system-wide, newest first.
    Never calls Gemini."""
    activities = [
        RecentActivityEntry(
            public_id=issue.public_id,
            category=issue.category,
            from_status=history_entry.from_status,
            to_status=history_entry.to_status,
            reason=history_entry.reason,
            changed_at=history_entry.changed_at,
        )
        for history_entry, issue in get_recent_activity(db, limit=limit)
    ]
    return RecentActivityResponse(activities=activities)


# --- Civic Accountability & Intelligence (Milestone 10) ----------------------


def get_civic_insights(db: Session) -> InsightsResponse:
    """Compute every grounded, deterministic civic insight.

    Purely deterministic -- never calls Gemini. Reuses existing Milestone
    8/9 aggregate queries (count_issues_by_status, get_department_workload,
    get_stale_issues) rather than re-querying the same data, plus two new
    small aggregate queries (count_active_high_severity_issues,
    count_active_issues_by_category) for signals M8/M9 didn't already
    compute. The actual "does this data support an insight" decisions live
    in backend/insights.py, kept separate and DB-free so those rules are
    unit-testable without a database.
    """
    insights: list[Insight] = []

    high_severity_insight = insight_rules.build_high_severity_insight(
        count_active_high_severity_issues(db)
    )
    if high_severity_insight is not None:
        insights.append(high_severity_insight)

    department_active_counts = [
        (
            department.code,
            department.name,
            sum(count for st, count in by_status.items() if st not in TERMINAL_STATUSES),
        )
        for department, by_status in get_department_workload(db)
    ]
    insights.extend(insight_rules.build_department_concentration_insights(department_active_counts))

    # Reuse the existing stale-issues query (Milestone 8) purely for its
    # `total` -- limit=1 avoids fetching the full item list we don't need
    # here.
    _stale_items, stale_total = repo_get_stale_issues(db, older_than_hours=48, limit=1, offset=0)
    stale_insight = insight_rules.build_stale_concentration_insight(
        stale_total, older_than_hours=48
    )
    if stale_insight is not None:
        insights.append(stale_insight)

    category_counts = {
        category.value: count for category, count in count_active_issues_by_category(db).items()
    }
    recurring_insight = insight_rules.build_recurring_category_insight(category_counts)
    if recurring_insight is not None:
        insights.append(recurring_insight)

    bottleneck_insight = insight_rules.build_lifecycle_bottleneck_insight(
        count_issues_by_status(db), TERMINAL_STATUSES
    )
    if bottleneck_insight is not None:
        insights.append(bottleneck_insight)

    return InsightsResponse(insights=insights, generated_at=datetime.now(timezone.utc))


def get_prioritized_insights(db: Session) -> PrioritizedInsightsResponse:
    """Compute grounded insights, then ask Gemini for an advisory priority
    order and explanation over them.

    ADVISORY ONLY: nothing here ever changes an Issue's status,
    assignment, or any official record -- see
    ai/prompts.py's PRIORITIZATION_SYSTEM_PROMPT for the enforced
    boundary. Only aggregate, non-identifying insight fields are sent to
    Gemini (never original_text, public_id, or confidence).

    If there are no insights, Gemini is never called (nothing to
    prioritize, and calling it anyway would risk fabricating a
    recommendation with no grounding). If Gemini is unavailable, fails, or
    returns something unusable, the grounded insights are still returned
    with ai_recommendation=None and a sanitized ai_recommendation_error --
    their value doesn't depend on Gemini succeeding.
    """
    insights_response = get_civic_insights(db)
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
    """Ask Gemini to explain a single issue's EXISTING classification, on
    demand, for an authority reviewing it. Returns None if no Issue
    matches public_id (route -> 404).

    NEVER PERSISTED: this reads the issue's already-extracted structured
    fields, calls Gemini fresh, and returns the result without writing
    anything back to the Issue or any other table -- no new column, no
    migration, nothing stored. Calling this twice in a row can produce
    two different (though similarly-grounded) explanations; that's
    expected and fine, since nothing here is authoritative.

    Only structured/extracted fields are sent to Gemini (category,
    problem, location, duration, affected_population, severity,
    confidence, suggested_department, assigned_department, status_label)
    -- never original_text or public_id, matching the same privacy
    discipline as get_prioritized_insights() above.

    Gemini is strictly advisory and explanatory here: it explains why the
    EXISTING classification/department make sense, and is instructed
    never to propose changing them (see EXPLANATION_SYSTEM_PROMPT). This
    function itself provides an additional, code-level guarantee on top
    of that prompt instruction -- it has no code path that writes to
    Issue, IssueStatusHistory, or IssueAssignmentHistory at all.

    If Gemini is unavailable, fails, or returns something unusable, a
    sanitized (never raw-exception) `error` is returned instead of
    fabricating an explanation -- this never blocks the rest of the
    authority workflow, since the issue detail itself doesn't depend on
    this endpoint succeeding.
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
