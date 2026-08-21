from typing import Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Path, Query, status
from google.genai.errors import APIError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ai.client import analyze_complaint
from ai.schemas import CivicIssue, IssueCategory, SeverityLevel
from backend.assignment_rules import (
    AssignmentNotAllowedError,
    DepartmentInactiveError,
    DepartmentNotFoundError,
)
from backend.database import get_db
from backend.models import PUBLIC_ID_PATTERN, Department, Issue, IssueStatus
from backend.repository import (
    get_active_departments,
    get_assignment_history,
    get_current_assignment,
    get_issue_by_public_id,
    get_status_history,
)
from backend.schemas import (
    AdminIssueDetailResponse,
    AdminIssueListResponse,
    AgingBucketsResponse,
    AnalyzeRequest,
    AssignmentHistoryEntryResponse,
    AssignmentRequest,
    AssignmentResponse,
    DashboardSummaryResponse,
    DepartmentResponse,
    DepartmentSummaryResponse,
    DepartmentWorkloadResponse,
    FunnelResponse,
    InsightsResponse,
    IssueResponse,
    PrioritizedInsightsResponse,
    PublicIssueTrackingResponse,
    RecentActivityResponse,
    ResolutionTimingResponse,
    SeverityDistributionResponse,
    StatusHistoryEntryResponse,
    StatusUpdateRequest,
)
from backend.service import (
    assign_issue_department,
    get_admin_issue_detail,
    get_aging_buckets,
    get_civic_insights,
    get_dashboard_summary,
    get_department_summary,
    get_department_workload_summary,
    get_prioritized_insights,
    get_public_tracking,
    get_recent_activity_feed,
    get_resolution_timing,
    get_severity_distribution,
    get_status_funnel,
    list_admin_issues,
    list_operational_queue,
    list_stale_issues,
    submit_complaint,
    transition_issue,
)
from backend.transitions import InvalidTransitionError

# Load environment variables from .env before anything else (e.g. ai/client.py)
# reads them. Must run before any AI service call, since GEMINI_API_KEY is
# read from the environment at call time.
load_dotenv()

app = FastAPI(
    title="CivicSync API",
    description="AI-powered civic intelligence platform API",
    version="0.1.0",
)


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "CivicSync API",
        "version": "0.1.0",
    }


@app.post(
    "/api/analyze",
    response_model=CivicIssue,
    status_code=status.HTTP_200_OK,
    summary="Analyze a citizen complaint into a structured civic issue",
)
def analyze(request: AnalyzeRequest) -> CivicIssue:
    """Convert raw citizen complaint text into a structured CivicIssue.

    All Gemini/AI logic lives in ai/client.py; this route only validates
    the request, delegates to the existing AI service, and translates
    known failure modes into clean HTTP responses.
    """
    try:
        return analyze_complaint(request.text)
    except ValueError as exc:
        # e.g. blank text (belt-and-suspenders alongside AnalyzeRequest's own
        # validation) or an empty/unparsable Gemini response.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from None
    except EnvironmentError:
        # Misconfiguration (e.g. missing GEMINI_API_KEY). Never echo the
        # underlying message back to the client -- it could reference env
        # var contents or local setup details.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AI service is not configured correctly.",
        ) from None
    except APIError:
        # Gemini-side failure (network, auth, rate limit, server error, etc).
        # google-genai's APIError messages can include request/response
        # internals, so respond with a generic message instead of str(exc).
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI service is temporarily unavailable. Please try again shortly.",
        ) from None


@app.post(
    "/api/issues",
    response_model=IssueResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a citizen complaint and persist it as a civic issue",
)
def submit_issue(request: AnalyzeRequest, db: Session = Depends(get_db)) -> Issue:
    """Analyze a citizen complaint and persist it as a new Issue (status
    SUBMITTED). All AI and database logic lives in backend/service.py and
    backend/repository.py; this route only validates the request,
    delegates, and translates known failure modes into HTTP responses.
    """
    try:
        return submit_complaint(db, request.text)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from None
    except EnvironmentError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AI service is not configured correctly.",
        ) from None
    except APIError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI service is temporarily unavailable. Please try again shortly.",
        ) from None
    except SQLAlchemyError:
        # Never echo str(exc) here -- SQLAlchemy error messages can include
        # the underlying SQL statement, connection details, or file paths.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save the issue. Please try again.",
        ) from None


@app.get(
    "/api/issues/{public_id}",
    response_model=IssueResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve a persisted civic issue by its public id",
    responses={status.HTTP_404_NOT_FOUND: {"description": "Issue not found."}},
)
def read_issue(public_id: str, db: Session = Depends(get_db)) -> Issue:
    """Look up a previously submitted Issue. Does not call Gemini."""
    issue = get_issue_by_public_id(db, public_id)
    if issue is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Issue not found.",
        )
    return issue


@app.post(
    "/api/issues/{public_id}/status",
    response_model=IssueResponse,
    status_code=status.HTTP_200_OK,
    summary="Transition a civic issue to a new status",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Issue not found."},
        status.HTTP_400_BAD_REQUEST: {
            "description": "The requested status transition is not permitted."
        },
    },
)
def update_issue_status(
    public_id: str, request: StatusUpdateRequest, db: Session = Depends(get_db)
) -> Issue:
    """Validate and apply a status transition, recording status history.

    Transition rules live in backend/transitions.py; persistence lives in
    backend/repository.py; backend/service.py orchestrates the lookup +
    transition. This route only translates known failure modes into HTTP
    responses. Does not call Gemini -- AI never drives status transitions.
    """
    try:
        issue = transition_issue(db, public_id, request.status, request.reason)
    except InvalidTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update issue status. Please try again.",
        ) from None

    if issue is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Issue not found.",
        )
    return issue


@app.get(
    "/api/issues/{public_id}/history",
    response_model=list[StatusHistoryEntryResponse],
    status_code=status.HTTP_200_OK,
    summary="Retrieve the status history for a civic issue",
    responses={status.HTTP_404_NOT_FOUND: {"description": "Issue not found."}},
)
def read_issue_status_history(
    public_id: str, db: Session = Depends(get_db)
) -> list[StatusHistoryEntryResponse]:
    """Return an Issue's status history, oldest first. Does not call Gemini."""
    issue = get_issue_by_public_id(db, public_id)
    if issue is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Issue not found.",
        )
    return get_status_history(db, issue)


@app.get(
    "/api/departments",
    response_model=list[DepartmentResponse],
    status_code=status.HTTP_200_OK,
    summary="List active departments in the controlled registry",
)
def list_departments(db: Session = Depends(get_db)) -> list[Department]:
    """Return active departments only. Does not call Gemini. The registry
    is not end-user-creatable here -- it's seeded once via Alembic."""
    return get_active_departments(db)


@app.post(
    "/api/issues/{public_id}/assignment",
    response_model=IssueResponse,
    status_code=status.HTTP_200_OK,
    summary="Officially assign (or reassign) a civic issue to a department",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Issue or department not found."},
        status.HTTP_400_BAD_REQUEST: {
            "description": (
                "Department is inactive, or the issue's current status "
                "doesn't permit assignment."
            )
        },
    },
)
def assign_issue(
    public_id: str, request: AssignmentRequest, db: Session = Depends(get_db)
) -> Issue:
    """Assign an issue to an official Department from the registry.

    This is always an explicit authorized action -- it is never derived
    from the AI's suggested_department. If the issue is CLASSIFIED, this
    also transitions it to ROUTED (via the existing centralized transition
    system); ROUTED-or-later issues are reassignable without altering
    their status. Transition rules, assignment eligibility, and
    persistence live in backend/transitions.py, backend/assignment_rules.py,
    and backend/repository.py; this route only translates known failure
    modes into HTTP responses. Does not call Gemini.
    """
    try:
        issue = assign_issue_department(db, public_id, request.department_code, request.reason)
    except DepartmentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from None
    except DepartmentInactiveError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from None
    except AssignmentNotAllowedError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to assign the issue. Please try again.",
        ) from None

    if issue is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Issue not found.",
        )
    return issue


@app.get(
    "/api/issues/{public_id}/assignment",
    response_model=AssignmentResponse | None,
    status_code=status.HTTP_200_OK,
    summary="Get the current official department assignment for an issue",
    responses={status.HTTP_404_NOT_FOUND: {"description": "Issue not found."}},
)
def read_current_assignment(public_id: str, db: Session = Depends(get_db)):
    """Return the current assignment, or a bare `200` with JSON `null` if
    the issue exists but has never been assigned -- that's a normal,
    expected state, not an error. 404 only if the issue itself doesn't
    exist. Does not call Gemini."""
    issue = get_issue_by_public_id(db, public_id)
    if issue is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Issue not found.",
        )
    current = get_current_assignment(db, issue)
    if current is None:
        return None
    return AssignmentResponse(
        code=current.department.code,
        name=current.department.name,
        assigned_at=current.assigned_at,
        reason=current.reason,
    )


@app.get(
    "/api/issues/{public_id}/assignment/history",
    response_model=list[AssignmentHistoryEntryResponse],
    status_code=status.HTTP_200_OK,
    summary="Get the full official assignment history for an issue",
    responses={status.HTTP_404_NOT_FOUND: {"description": "Issue not found."}},
)
def read_assignment_history(
    public_id: str, db: Session = Depends(get_db)
) -> list[AssignmentHistoryEntryResponse]:
    """Return all assignment history entries, oldest first. Does not call
    Gemini."""
    issue = get_issue_by_public_id(db, public_id)
    if issue is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Issue not found.",
        )
    return [
        AssignmentHistoryEntryResponse(
            department_code=entry.department.code,
            department_name=entry.department.name,
            assigned_at=entry.assigned_at,
            unassigned_at=entry.unassigned_at,
            reason=entry.reason,
        )
        for entry in get_assignment_history(db, issue)
    ]


@app.get(
    "/api/track/{public_id}",
    response_model=PublicIssueTrackingResponse,
    status_code=status.HTTP_200_OK,
    summary="Track a civic issue",
    description=(
        "Public, unauthenticated endpoint for a citizen to track the status of "
        "a previously submitted civic issue by its public id. Returns only "
        "citizen-safe information -- internal database ids, AI confidence, the "
        "raw AI department recommendation, and the original complaint text are "
        "never exposed here. Read-only; never calls Gemini."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "No issue found with that public id."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Malformed public id (expected format: CIV-<12 uppercase hex characters>)."
        },
    },
)
def track_issue(
    public_id: str = Path(
        ...,
        pattern=PUBLIC_ID_PATTERN,
        description="CivicSync public issue id, e.g. CIV-AFFDA97DDA53.",
        examples=["CIV-AFFDA97DDA53"],
    ),
    db: Session = Depends(get_db),
) -> PublicIssueTrackingResponse:
    """Public citizen tracking. The public_id format is validated by the
    path parameter's pattern above (422 on mismatch) *before* any database
    query runs, so obviously malformed ids never reach the database.
    Response construction (what's citizen-safe, timeline derivation) lives
    in backend.service.get_public_tracking -- this route only handles the
    404 case."""
    tracking = get_public_tracking(db, public_id)
    if tracking is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No issue found with that tracking id.",
        )
    return tracking


# ============================================================================
# Authority Operations API (Milestone 8) -- INTERNAL/AUTHORITY, NOT PUBLIC.
#
# IMPORTANT: these routes are NOT authenticated yet. Authentication and
# authorization will be added before any production deployment -- do not
# treat these endpoints as secure, and do not expose them to untrusted
# clients until that lands. Every route below is read-only and never calls
# Gemini.
# ============================================================================


@app.get(
    "/api/admin/issues",
    response_model=AdminIssueListResponse,
    status_code=status.HTTP_200_OK,
    summary="[INTERNAL/AUTHORITY] List and filter civic issues",
    description=(
        "INTERNAL AUTHORITY API -- not authenticated yet; do not expose to "
        "untrusted clients. Supplied filters (status, department, "
        "severity, category) are ANDed together. department_code filters "
        "by the OFFICIAL assigned department, never the AI's "
        "suggested_department. Filtering, sorting, and pagination all "
        "happen at the database level. Never calls Gemini."
    ),
)
def list_admin_issues_route(
    status_filter: IssueStatus | None = Query(
        default=None, alias="status", description="Filter by exact lifecycle status."
    ),
    department_code: str | None = Query(
        default=None,
        description="Filter by the OFFICIAL assigned department code (not the AI suggestion).",
    ),
    severity: SeverityLevel | None = Query(default=None, description="Filter by severity."),
    category: IssueCategory | None = Query(
        default=None, description="Filter by AI-assigned category."
    ),
    sort_by: Literal["updated_at", "created_at", "severity"] = Query(
        default="updated_at",
        description=(
            "Sort field. 'severity' sorts by an explicit priority ordering "
            "(CRITICAL, HIGH, MEDIUM, LOW, UNKNOWN), not alphabetically."
        ),
    ),
    sort_order: Literal["asc", "desc"] = Query(default="desc"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> AdminIssueListResponse:
    return list_admin_issues(
        db,
        status=status_filter,
        department_code=department_code,
        severity=severity,
        category=category,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
    )


@app.get(
    "/api/admin/issues/stale",
    response_model=AdminIssueListResponse,
    status_code=status.HTTP_200_OK,
    summary="[INTERNAL/AUTHORITY] List stale/aging issues",
    description=(
        "INTERNAL AUTHORITY API -- not authenticated yet. Returns issues "
        "whose updated_at is at least older_than_hours old (default 48). "
        "This is a simple 'now - updated_at >= older_than_hours' check, "
        "not an SLA system. Registered before the /{public_id} detail "
        "route below so 'stale' is never matched as a public_id. Read-only; "
        "never mutates data; never calls Gemini."
    ),
)
def list_stale_issues_route(
    older_than_hours: int = Query(
        default=48, ge=1, description="Minimum hours since the issue's last update."
    ),
    department_code: str | None = Query(
        default=None, description="Filter by the official assigned department code."
    ),
    status_filter: IssueStatus | None = Query(
        default=None, alias="status", description="Filter by exact lifecycle status."
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> AdminIssueListResponse:
    return list_stale_issues(
        db,
        older_than_hours=older_than_hours,
        department_code=department_code,
        status=status_filter,
        limit=limit,
        offset=offset,
    )


@app.get(
    "/api/admin/issues/{public_id}",
    response_model=AdminIssueDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="[INTERNAL/AUTHORITY] Full issue detail",
    description=(
        "INTERNAL AUTHORITY API -- not authenticated yet; do not expose to "
        "untrusted clients. Exposes operational detail withheld from the "
        "public tracking endpoint: original complaint text, AI confidence, "
        "the AI's suggested_department, and complete status/assignment "
        "history. Still never exposes internal database ids. Never calls "
        "Gemini."
    ),
    responses={status.HTTP_404_NOT_FOUND: {"description": "No issue found with that public id."}},
)
def read_admin_issue_detail(public_id: str, db: Session = Depends(get_db)) -> AdminIssueDetailResponse:
    detail = get_admin_issue_detail(db, public_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Issue not found.",
        )
    return detail


@app.get(
    "/api/admin/dashboard/summary",
    response_model=DashboardSummaryResponse,
    status_code=status.HTTP_200_OK,
    summary="[INTERNAL/AUTHORITY] Dashboard aggregate counts",
    description=(
        "INTERNAL AUTHORITY API -- not authenticated yet. All counts are "
        "computed with SQL GROUP BY aggregation, never by loading every "
        "issue into Python. by_status and by_severity include every known "
        "enum value (zero-filled); by_department includes every active "
        "department (zero-filled). Never calls Gemini."
    ),
)
def read_dashboard_summary(db: Session = Depends(get_db)) -> DashboardSummaryResponse:
    return get_dashboard_summary(db)


@app.get(
    "/api/admin/departments/{department_code}/summary",
    response_model=DepartmentSummaryResponse,
    status_code=status.HTTP_200_OK,
    summary="[INTERNAL/AUTHORITY] Per-department operational summary",
    description=(
        "INTERNAL AUTHORITY API -- not authenticated yet. All counts scope "
        "to issues CURRENTLY assigned to this department (not historical "
        "assignments). active_issues excludes the terminal statuses CLOSED "
        "and REJECTED, using the existing lifecycle semantics -- no new "
        "status is introduced. Never calls Gemini."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "No department found with that code."}
    },
)
def read_department_summary(
    department_code: str, db: Session = Depends(get_db)
) -> DepartmentSummaryResponse:
    summary = get_department_summary(db, department_code)
    if summary is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Department not found.",
        )
    return summary


@app.get(
    "/api/admin/queue",
    response_model=AdminIssueListResponse,
    status_code=status.HTTP_200_OK,
    summary="[INTERNAL/AUTHORITY] Operational queue",
    description=(
        "INTERNAL AUTHORITY API -- not authenticated yet. Issues requiring "
        "operational attention: every status except the terminal CLOSED "
        "and REJECTED. Ordered by an explicit severity priority (CRITICAL, "
        "HIGH, MEDIUM, LOW, UNKNOWN -- not alphabetical), then oldest "
        "updated_at first, both computed in SQL. Never calls Gemini."
    ),
)
def read_operational_queue(
    department_code: str | None = Query(
        default=None, description="Filter by the official assigned department code."
    ),
    severity: SeverityLevel | None = Query(default=None, description="Filter by severity."),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> AdminIssueListResponse:
    return list_operational_queue(
        db, department_code=department_code, severity=severity, limit=limit, offset=offset
    )


# ============================================================================
# Civic Intelligence & Analytics (Milestone 9) -- INTERNAL/AUTHORITY, NOT
# PUBLIC. Same caveat as the rest of the Authority Operations API above:
# NOT authenticated yet. Every route below is read-only and never calls
# Gemini.
# ============================================================================


@app.get(
    "/api/admin/analytics/funnel",
    response_model=FunnelResponse,
    status_code=status.HTTP_200_OK,
    summary="[INTERNAL/AUTHORITY] Status funnel",
    description=(
        "INTERNAL AUTHORITY API -- not authenticated yet. Dashboard-ready "
        "status funnel: every IssueStatus in lifecycle-declaration order "
        "with its current count. Backed by the same SQL GROUP BY query as "
        "GET /api/admin/dashboard/summary's by_status -- this endpoint just "
        "presents it as an ordered list for a funnel chart. Never calls "
        "Gemini."
    ),
)
def read_status_funnel(db: Session = Depends(get_db)) -> FunnelResponse:
    return get_status_funnel(db)


@app.get(
    "/api/admin/analytics/severity-distribution",
    response_model=SeverityDistributionResponse,
    status_code=status.HTTP_200_OK,
    summary="[INTERNAL/AUTHORITY] Severity distribution",
    description=(
        "INTERNAL AUTHORITY API -- not authenticated yet. Severity "
        "breakdown with percentages, using the existing SeverityLevel "
        "enum only (no second severity vocabulary). Never calls Gemini."
    ),
)
def read_severity_distribution(db: Session = Depends(get_db)) -> SeverityDistributionResponse:
    return get_severity_distribution(db)


@app.get(
    "/api/admin/analytics/department-workload",
    response_model=DepartmentWorkloadResponse,
    status_code=status.HTTP_200_OK,
    summary="[INTERNAL/AUTHORITY] Department workload",
    description=(
        "INTERNAL AUTHORITY API -- not authenticated yet. Status breakdown "
        "and active/resolved/closed rollup for EVERY active department in "
        "one call, based on the OFFICIAL assigned department (never "
        "suggested_department). One SQL query for all departments -- "
        "no N+1. Never calls Gemini."
    ),
)
def read_department_workload(db: Session = Depends(get_db)) -> DepartmentWorkloadResponse:
    return get_department_workload_summary(db)


@app.get(
    "/api/admin/analytics/aging",
    response_model=AgingBucketsResponse,
    status_code=status.HTTP_200_OK,
    summary="[INTERNAL/AUTHORITY] Issue aging buckets",
    description=(
        "INTERNAL AUTHORITY API -- not authenticated yet. Age-since-"
        "created_at distribution for currently ACTIVE issues (excludes "
        "CLOSED/REJECTED). Plain, neutral duration buckets computed in SQL "
        "-- not SLA targets; no bucket implies an issue is 'on time' or "
        "'overdue'. Never calls Gemini."
    ),
)
def read_aging_buckets(db: Session = Depends(get_db)) -> AgingBucketsResponse:
    return get_aging_buckets(db)


@app.get(
    "/api/admin/analytics/resolution-timing",
    response_model=ResolutionTimingResponse,
    status_code=status.HTTP_200_OK,
    summary="[INTERNAL/AUTHORITY] Resolution/closure timing",
    description=(
        "INTERNAL AUTHORITY API -- not authenticated yet. Average time to "
        "resolve and to close, computed only over issues that actually "
        "have the relevant timestamp(s) -- an average is null (never a "
        "misleading 0) if no issue has reached that stage yet. Never calls "
        "Gemini."
    ),
)
def read_resolution_timing(db: Session = Depends(get_db)) -> ResolutionTimingResponse:
    return get_resolution_timing(db)


@app.get(
    "/api/admin/analytics/recent-activity",
    response_model=RecentActivityResponse,
    status_code=status.HTTP_200_OK,
    summary="[INTERNAL/AUTHORITY] Recent civic activity feed",
    description=(
        "INTERNAL AUTHORITY API -- not authenticated yet. Most recent "
        "status-transition events across ALL issues, newest first -- a "
        "single JOIN query, not one lookup per event. Never calls Gemini."
    ),
)
def read_recent_activity(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> RecentActivityResponse:
    return get_recent_activity_feed(db, limit=limit)


# ============================================================================
# Civic Accountability & Intelligence (Milestone 10) -- INTERNAL/AUTHORITY,
# NOT PUBLIC. Same caveat as the rest of the Authority Operations API above:
# NOT authenticated yet.
#
# AI SAFETY BOUNDARY: POST /api/admin/insights/prioritize is the only route
# in this entire application (besides the existing POST /api/analyze and
# POST /api/issues) that calls Gemini. Gemini's role here is strictly
# advisory -- it reorders and explains insights CivicSync already computed
# deterministically. It cannot and does not change any Issue's status,
# department assignment, or any official record; those still only happen
# through the existing lifecycle/assignment endpoints, driven by a human
# authority. See ai/prompts.py's PRIORITIZATION_SYSTEM_PROMPT and
# backend/service.py's get_prioritized_insights() for the enforced boundary.
# ============================================================================


@app.get(
    "/api/admin/insights",
    response_model=InsightsResponse,
    status_code=status.HTTP_200_OK,
    summary="[INTERNAL/AUTHORITY] Grounded civic insights",
    description=(
        "INTERNAL AUTHORITY API -- not authenticated yet. Structured, "
        "explainable insights (high-severity unresolved workload, "
        "department workload concentration, stale-issue concentration, "
        "recurring categories, lifecycle bottlenecks) computed entirely "
        "with deterministic SQL aggregation over existing data. Every "
        "insight's `evidence` traces back to real counts -- nothing is "
        "fabricated, and an insight is only included when the underlying "
        "data actually supports it. Purely deterministic; never calls "
        "Gemini."
    ),
)
def read_civic_insights(db: Session = Depends(get_db)) -> InsightsResponse:
    return get_civic_insights(db)


@app.post(
    "/api/admin/insights/prioritize",
    response_model=PrioritizedInsightsResponse,
    status_code=status.HTTP_200_OK,
    summary="[INTERNAL/AUTHORITY] AI-assisted insight prioritization (advisory only)",
    description=(
        "INTERNAL AUTHORITY API -- not authenticated yet. Computes the same "
        "grounded insights as GET /api/admin/insights, then asks Gemini to "
        "recommend a priority order and explain it. ADVISORY ONLY: Gemini "
        "never changes any issue's status, department assignment, or any "
        "official record -- an authority must act through the existing "
        "lifecycle/assignment endpoints. Only aggregate, non-identifying "
        "insight data is sent to Gemini (never original complaint text, "
        "public_id, or confidence). If there are no insights, Gemini is "
        "never called. If Gemini is unavailable or fails, the grounded "
        "insights are still returned with ai_recommendation=null rather "
        "than failing the whole request."
    ),
)
def prioritize_civic_insights(db: Session = Depends(get_db)) -> PrioritizedInsightsResponse:
    return get_prioritized_insights(db)