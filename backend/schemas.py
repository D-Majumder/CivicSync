"""
API request/response models for the FastAPI layer.

Kept separate from ai/schemas.py: these describe the HTTP contract for
backend/main.py, while ai/schemas.py describes the AI service's own data
model (CivicIssue). CivicIssue is reused directly as the response model
for POST /api/analyze (AI-only, not persisted).
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from ai.schemas import IssueCategory, SeverityLevel
from backend.models import IssueStatus, JurisdictionLevel, ReopenRequestState


def _tag_naive_datetime_as_utc(value: Any) -> Any:
    """Attach explicit UTC tzinfo to a naive datetime.

    backend/models.py always writes timestamps via datetime.now(timezone.utc)
    (genuinely UTC), but SQLite's DATETIME columns don't persist a timezone
    offset -- reading a row back gives a *naive* datetime whose wall-clock
    value is UTC, with no marker saying so. Serialized as-is, that produces
    an ambiguous ISO string like "2026-08-21T07:21:25" (no "Z"/offset),
    which JavaScript's `new Date(...)` -- per the ECMA-262 spec -- parses
    as LOCAL time rather than UTC, silently corrupting every displayed
    timestamp by the viewer's UTC offset.

    This validator is the fix at the source: it runs before Pydantic's own
    datetime parsing, so every API response ends up with an explicit
    "+00:00"/"Z" suffix and is unambiguous to any client. It never changes
    the underlying value (the wall-clock digits are untouched) and never
    touches the database, a migration, or business logic -- only how an
    already-correct value gets *labeled* on its way out over the wire.
    """
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# Use this instead of `datetime` for every timestamp field in this file.
# See _tag_naive_datetime_as_utc's docstring for why it exists.
UtcDatetime = Annotated[datetime, BeforeValidator(_tag_naive_datetime_as_utc)]


class AnalyzeRequest(BaseModel):
    """Request body for POST /api/analyze and POST /api/issues.

    Both endpoints start from the same citizen-complaint text, so this one
    model covers both requests.
    """

    text: str = Field(
        ...,
        min_length=1,
        description="Raw citizen complaint text (voice-transcribed or typed) to analyze.",
        examples=["There has been no street light near our school for two weeks."],
    )


class DepartmentSummary(BaseModel):
    """Minimal public department representation, nested inside IssueResponse.

    Deliberately excludes the department's internal integer id -- code is
    the only identifier ever exposed externally.
    """

    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str


class IssueResponse(BaseModel):
    """Public API representation of a persisted civic issue.

    Deliberately excludes the internal integer database id and any other
    SQLAlchemy implementation detail -- public_id is the only identifier
    ever exposed externally. Built with from_attributes=True so it can be
    populated directly from a backend.models.Issue ORM instance.
    """

    model_config = ConfigDict(from_attributes=True)

    public_id: str
    original_text: str
    category: IssueCategory
    problem: str
    location: str | None
    duration: str | None
    severity: SeverityLevel
    affected_population: str | None
    suggested_department: str | None
    confidence: float
    # The official assignment, as a structured Department reference (code +
    # name) -- never the raw assigned_department_id, and never derived
    # from suggested_department above. None if never officially assigned.
    assigned_department: DepartmentSummary | None
    status: IssueStatus
    resolution_summary: str | None
    resolved_by: str | None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    resolved_at: UtcDatetime | None
    closed_at: UtcDatetime | None


class StatusUpdateRequest(BaseModel):
    """Request body for POST /api/issues/{public_id}/status.

    `status` reuses the IssueStatus enum directly, so an unrecognized
    status string is rejected by validation (422) before the route ever
    runs -- the transition-legality check (400) happens separately, in
    backend/transitions.py.
    """

    status: IssueStatus = Field(..., description="Requested new status for the issue.")
    reason: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional human-readable reason for this transition.",
        examples=["Repair crew has started replacing the damaged light."],
    )


class ResolveIssueRequest(BaseModel):
    """Request body for POST /api/issues/{public_id}/resolve (Milestone
    18, Phase 2).

    Deliberately narrower than StatusUpdateRequest: this endpoint only
    ever transitions an issue to RESOLVED (enforced server-side by the
    same backend.transitions validator the generic status endpoint uses
    -- an issue not currently IN_PROGRESS is rejected with 400, the same
    as any other illegal transition), and `resolution_note` is REQUIRED
    and validated as meaningful, unlike the generic endpoint's optional
    `reason`. There is no client-supplied timestamp or resolver identity
    field here on purpose -- resolved_at and resolved_by are always
    server-generated (see backend/service.py's resolve_issue).
    """

    resolution_note: str = Field(
        ...,
        min_length=3,
        max_length=2000,
        description="Required description of how the issue was actually resolved.",
        examples=["Repair crew replaced the damaged street light fixture on-site."],
    )

    @field_validator("resolution_note")
    @classmethod
    def _reject_whitespace_only(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) < 3:
            raise ValueError(
                "resolution_note must contain a meaningful description, not just whitespace."
            )
        return stripped


class ReopenRequestCreate(BaseModel):
    """Request body for POST /api/track/{public_id}/reopen-request
    (Milestone 18, Phase 5) -- the citizen-facing endpoint.

    Mirrors ResolveIssueRequest's exact validation pattern: `reason` is
    REQUIRED and validated as meaningful, not just present. There is no
    status field here at all -- a citizen can never directly set an
    issue's status; this only ever creates a PENDING request for an
    authority to review (see backend/service.py's request_issue_reopen).
    """

    reason: str = Field(
        ...,
        min_length=3,
        max_length=2000,
        description="Required explanation of why the resolution was inadequate or the issue has returned.",
        examples=["The pothole has reappeared after only two weeks."],
    )

    @field_validator("reason")
    @classmethod
    def _reject_whitespace_only(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) < 3:
            raise ValueError("reason must contain a meaningful explanation, not just whitespace.")
        return stripped


class ReopenDecisionRequest(BaseModel):
    """Request body for POST /api/admin/reopen-requests/{request_public_id}/decision
    (Milestone 18, Phase 5) -- the authority-facing review endpoint.

    `approve` chooses the outcome; `decision_reason` is optional in both
    directions (an authority may approve/reject with or without
    additional commentary beyond the citizen's own stated reason).
    """

    approve: bool = Field(..., description="True to approve and reopen the issue, False to reject.")
    decision_reason: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional authority commentary on the decision.",
        examples=["Confirmed the pothole has reappeared; reopening for re-repair."],
    )


class ReopenRequestResponse(BaseModel):
    """Response shape for reopen-request endpoints, used for BOTH the
    citizen and authority views (Milestone 18, Phase 5) -- there's
    nothing authority-only worth exposing here beyond what a citizen
    already knows about their own request.

    Deliberately excludes decided_by (the reviewing authority's identity
    stays authority-only, matching Issue.resolved_by's own treatment)
    and the internal integer id/issue_id -- public_id is the only
    identifier ever exposed.
    """

    public_id: str
    state: ReopenRequestState
    reason: str
    created_at: UtcDatetime
    decision_reason: str | None
    decided_at: UtcDatetime | None


class StatusHistoryEntryResponse(BaseModel):
    """Public API representation of a single status-history row.

    Excludes the row's own id and issue_id -- callers only ever look up
    history by an Issue's public_id, never by these internal ids.
    """

    model_config = ConfigDict(from_attributes=True)

    from_status: IssueStatus | None
    to_status: IssueStatus
    reason: str | None
    changed_at: UtcDatetime


class DepartmentResponse(BaseModel):
    """Public API representation of a department, for GET /api/departments.

    Excludes the department's internal integer id.
    """

    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    description: str | None
    is_active: bool


class AssignmentRequest(BaseModel):
    """Request body for POST /api/issues/{public_id}/assignment.

    department_code is validated against the department registry (404 if
    unknown, 400 if inactive) in backend/service.py -- it isn't a fixed
    Python enum here because the registry is meant to be a controlled but
    data-driven source of truth, not something baked into the API schema.
    """

    department_code: str = Field(
        ...,
        min_length=1,
        description="Code of the department to assign this issue to.",
        examples=["STREET_LIGHTING"],
    )
    reason: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional human-readable reason for this assignment.",
        examples=["Complaint routed to the department responsible for street lighting."],
    )


class AssignmentResponse(BaseModel):
    """Current assignment for an issue.

    GET /api/issues/{public_id}/assignment returns this wrapped in `200`,
    or a bare `200` with a JSON `null` body if the issue exists but has
    never been assigned -- "never assigned" is a normal, expected state
    for an issue, not an error, so it doesn't get a 404.
    """

    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    assigned_at: UtcDatetime
    reason: str | None


class AssignmentHistoryEntryResponse(BaseModel):
    """Public API representation of a single assignment-history row.

    Excludes the row's own id, issue_id, and department_id -- department
    identity is exposed via department_code/department_name instead.
    """

    department_code: str
    department_name: str
    assigned_at: UtcDatetime
    unassigned_at: UtcDatetime | None
    reason: str | None


class PublicTimelineEntry(BaseModel):
    """One citizen-facing timeline entry, derived from a status-history row.

    Deliberately narrower than StatusHistoryEntryResponse (the internal/
    admin representation): from_status is omitted entirely -- citizens see
    what state the issue reached and when, not the raw before/after pair
    (and the initial history row's from_status is null anyway, which we
    never expose). `status` here is the row's to_status.
    """

    status: IssueStatus
    timestamp: UtcDatetime
    reason: str | None


class PublicEvidenceSummary(BaseModel):
    """Citizen-safe evidence metadata for GET /api/track/{public_id}
    (Milestone 18, Phase 4).

    Deliberately narrower than the authority-facing EvidenceResponse:
    omits uploaded_by (the resolving authority's identity stays
    authority-only, matching resolved_by's own treatment below) and
    content_type/size_bytes (not needed by the public UI). The actual
    file is retrieved separately via
    GET /api/track/{public_id}/evidence/{evidence_public_id}/file, which
    verifies the evidence belongs to that same publicly-tracked issue
    before returning anything -- see backend.service.get_public_evidence_file.
    """

    public_id: str
    original_filename: str
    uploaded_at: UtcDatetime


class PublicIssueTrackingResponse(BaseModel):
    """Public, citizen-safe tracking representation for
    GET /api/track/{public_id}.

    Deliberately a distinct model from IssueResponse, not a trimmed-down
    reuse of it: IssueResponse is the internal/admin representation and
    includes original_text, confidence, suggested_department, and
    resolved_by -- none of which belong on an unauthenticated public
    endpoint. This model is always built explicitly (see
    backend.service.get_public_tracking), never populated directly from
    an Issue ORM instance via from_attributes, so a future addition to
    Issue can never silently leak through here.

    resolution_summary/resolved_at ARE exposed (Milestone 18, Phase 4) --
    a citizen is entitled to know how and when their own report was
    resolved. resolved_by is deliberately NOT exposed here -- the
    resolving authority's identity stays authority-only, the same
    treatment as every other internal-operations field this model omits.
    """

    public_id: str
    category: IssueCategory
    problem: str
    location: str | None
    duration: str | None
    affected_population: str | None
    severity: SeverityLevel
    status: IssueStatus
    # Citizen-friendly label alongside the machine-readable status (e.g.
    # "In Progress" for IN_PROGRESS) -- IssueStatus itself is unchanged.
    status_label: str
    assigned_department: DepartmentSummary | None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    resolution_summary: str | None
    resolved_at: UtcDatetime | None
    timeline: list[PublicTimelineEntry]
    evidence: list[PublicEvidenceSummary]
    active_reopen_request: ReopenRequestResponse | None


# --- Authority operations (Milestone 8) --------------------------------------
#
# These are deliberately separate from PublicIssueTrackingResponse and its
# nested models -- the authority list/detail responses expose materially
# more (original_text, confidence, suggested_department, full history)
# than what's appropriate for the unauthenticated public tracking
# endpoint. Every one of these is built explicitly in backend/service.py,
# never populated from an Issue ORM instance via from_attributes, so a
# future Issue column can't silently leak into an admin response either.


class AdminIssueListItem(BaseModel):
    """One row in an authority issue list (GET /api/admin/issues,
    /api/admin/queue, /api/admin/issues/stale). More than the public
    tracking endpoint exposes, but still never the internal integer id."""

    public_id: str
    category: IssueCategory
    problem: str
    severity: SeverityLevel
    status: IssueStatus
    status_label: str
    assigned_department: DepartmentSummary | None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class AdminIssueListResponse(BaseModel):
    """Paginated list response shared by GET /api/admin/issues,
    GET /api/admin/queue, and GET /api/admin/issues/stale -- all three are
    "a filtered, paginated list of issues," just with different default
    filters, so they share this one response shape."""

    items: list[AdminIssueListItem]
    total: int
    limit: int
    offset: int


class AdminStatusHistoryEntry(BaseModel):
    """Status-history row for the authority detail endpoint. Unlike the
    public timeline, this keeps from_status (authorities see the full
    before/after transition, not just the resulting state) -- but still
    excludes the row's own id and issue_id."""

    from_status: IssueStatus | None
    to_status: IssueStatus
    reason: str | None
    changed_at: UtcDatetime


class AdminAssignmentHistoryEntry(BaseModel):
    """Assignment-history row for the authority detail endpoint. Excludes
    the row's own id, issue_id, and department_id -- department identity
    is exposed via department_code/department_name instead."""

    department_code: str
    department_name: str
    assigned_at: UtcDatetime
    unassigned_at: UtcDatetime | None
    reason: str | None


class EvidenceResponse(BaseModel):
    """Metadata for one resolution evidence file (Milestone 18, Phase 3).

    Deliberately excludes storage_key (an internal implementation detail
    of where the file physically lives -- see backend/evidence_storage.py)
    and the internal integer id/issue_id -- public_id is the only
    identifier ever exposed, same convention as Issue.public_id. The
    actual file bytes are retrieved separately via
    GET /api/evidence/{public_id}/file, never a raw filesystem path.
    """

    public_id: str
    original_filename: str
    content_type: str
    size_bytes: int
    uploaded_by: str
    uploaded_at: UtcDatetime


class AdminIssueDetailResponse(BaseModel):
    """Full authority-facing detail for GET /api/admin/issues/{public_id}.

    Exposes operational information the public tracking endpoint
    deliberately withholds: original_text, confidence,
    suggested_department, resolution_summary, and complete status/
    assignment history. Still never exposes the internal integer id or
    any history row's own id/issue_id/department_id.
    """

    public_id: str
    original_text: str
    category: IssueCategory
    problem: str
    location: str | None
    duration: str | None
    affected_population: str | None
    suggested_department: str | None
    confidence: float
    severity: SeverityLevel
    assigned_department: DepartmentSummary | None
    status: IssueStatus
    status_label: str
    resolution_summary: str | None
    resolved_by: str | None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    resolved_at: UtcDatetime | None
    closed_at: UtcDatetime | None
    status_history: list[AdminStatusHistoryEntry]
    assignment_history: list[AdminAssignmentHistoryEntry]
    evidence: list[EvidenceResponse]
    pending_reopen_request: ReopenRequestResponse | None


class DepartmentIssueCount(BaseModel):
    """One row of GET /api/admin/dashboard/summary's by_department list."""

    code: str
    name: str
    count: int


class CategoryReopenCount(BaseModel):
    """One row of ResolutionIntelligenceResponse.approved_reopens_by_category."""

    category: IssueCategory
    count: int


class ResolutionIntelligenceResponse(BaseModel):
    """Resolution, reopening, and evidence-coverage KPIs for
    GET /api/admin/analytics/resolution-intelligence (Milestone 19).

    Every rate/percentage field is None (never 0 or a fabricated value)
    when its denominator is zero -- e.g. resolution_rate_percent is None
    for a jurisdiction with no non-rejected issues yet, rather than a
    misleading 0%. total_resolved counts issues that have EVER been
    resolved (resolved_at IS NOT NULL), including ones later reopened --
    see backend.repository.get_resolution_intelligence for the full
    computation.
    """

    total_issues: int
    total_resolved: int
    resolution_rate_percent: float | None
    avg_resolution_hours: float | None
    median_resolution_hours: float | None
    currently_reopened: int
    total_reopen_requests: int
    pending_reopen_requests: int
    approved_reopen_requests: int
    rejected_reopen_requests: int
    reopen_rate_percent: float | None
    resolutions_with_evidence: int
    resolutions_without_evidence: int
    evidence_coverage_percent: float | None
    approved_reopens_by_category: list[CategoryReopenCount]


class DashboardSummaryResponse(BaseModel):
    """Aggregate operational counts for GET /api/admin/dashboard/summary.

    by_status and by_severity always include every known enum value
    (zero-filled), and by_department always includes every active
    department (zero-filled) -- see backend/repository.py's
    count_issues_by_status/severity/department, all SQL GROUP BY queries.
    """

    total_issues: int
    by_status: dict[IssueStatus, int]
    by_severity: dict[SeverityLevel, int]
    by_department: list[DepartmentIssueCount]


class DepartmentSummaryResponse(BaseModel):
    """Per-department operational summary for
    GET /api/admin/departments/{department_code}/summary.

    All counts scope to issues CURRENTLY assigned to this department
    (Issue.assigned_department), not historical assignments.
    active_issues excludes the terminal statuses CLOSED and REJECTED.
    """

    code: str
    name: str
    total_assigned_issues: int
    by_status: dict[IssueStatus, int]
    by_severity: dict[SeverityLevel, int]
    active_issues: int
    resolved_issues: int
    closed_issues: int


# --- Civic Intelligence & Analytics (Milestone 9) ----------------------------
#
# Every model here is built explicitly in backend/service.py from
# repository query results -- never from_attributes on an Issue ORM
# object -- consistent with how the Milestone 8 admin/public models work.


class FunnelStage(BaseModel):
    """One stage of the status funnel, in lifecycle-declaration order."""

    status: IssueStatus
    status_label: str
    count: int


class FunnelResponse(BaseModel):
    """Dashboard-ready status funnel for GET
    /api/admin/analytics/funnel. Reuses the same per-status counts as
    GET /api/admin/dashboard/summary (no duplicate query), presented as
    an ordered list suitable for a funnel chart rather than a dict.
    """

    stages: list[FunnelStage]
    total_issues: int


class SeverityDistributionEntry(BaseModel):
    severity: SeverityLevel
    count: int
    percentage: float


class SeverityDistributionResponse(BaseModel):
    """Severity breakdown with percentages, using the existing
    SeverityLevel enum -- no second severity vocabulary."""

    distribution: list[SeverityDistributionEntry]
    total_issues: int


class DepartmentWorkloadEntry(BaseModel):
    """One department's current workload: status breakdown plus the
    active/resolved/closed rollup (same terminal-status definition used
    throughout the app: active = not CLOSED/REJECTED)."""

    code: str
    name: str
    total_assigned: int
    active_issues: int
    resolved_issues: int
    closed_issues: int
    by_status: dict[IssueStatus, int]


class DepartmentWorkloadResponse(BaseModel):
    """Workload for every active department in one response -- avoids
    needing one call per department to build this picture."""

    departments: list[DepartmentWorkloadEntry]


class AgingBucketEntry(BaseModel):
    bucket: str
    count: int


class AgingBucketsResponse(BaseModel):
    """Age-since-submission distribution for currently active issues.
    Neutral duration buckets -- not an SLA judgment."""

    buckets: list[AgingBucketEntry]
    total_active_issues: int


class ResolutionTimingResponse(BaseModel):
    """Resolution/closure timing. Each average is null if no issue has
    the relevant timestamp(s) yet, never a misleading 0."""

    resolved_issue_count: int
    avg_time_to_resolve_hours: float | None
    closed_issue_count: int
    avg_time_to_close_after_resolution_hours: float | None
    avg_time_to_close_from_creation_hours: float | None


class RecentActivityEntry(BaseModel):
    """One system-wide status-transition event for the authority
    dashboard's activity feed. Excludes internal history/issue ids."""

    public_id: str
    category: IssueCategory
    from_status: IssueStatus | None
    to_status: IssueStatus
    reason: str | None
    changed_at: UtcDatetime


class RecentActivityResponse(BaseModel):
    activities: list[RecentActivityEntry]


# --- Civic Accountability & Intelligence (Milestone 10) ----------------------
#
# Insight/AI-recommendation models are built explicitly in
# backend/insights.py (deterministic) and backend/service.py (AI
# orchestration) -- never from_attributes on an ORM object.


class InsightPriority(str, Enum):
    """How urgently an insight warrants an authority's attention. Distinct
    from SeverityLevel (an AI-assessed property of a single issue) --
    this is CivicSync's own assessment of an aggregate pattern."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Insight(BaseModel):
    """A single grounded, structured civic insight.

    Every field traces back to real database aggregates computed in
    backend/repository.py and backend/insights.py -- nothing here is
    fabricated. `ai_generated` is always False for insights returned by
    GET /api/admin/insights (purely deterministic); it exists so the same
    model can, in principle, represent an AI-originated insight later
    without a schema change, though Milestone 10 never sets it True here.
    """

    insight_type: str
    priority: InsightPriority
    title: str
    summary: str
    affected_issue_count: int
    affected_department: DepartmentSummary | None
    evidence: dict[str, int | float | str]
    recommended_action: str
    ai_generated: bool = False


class InsightsResponse(BaseModel):
    insights: list[Insight]
    generated_at: UtcDatetime


class AIPrioritizationRecommendation(BaseModel):
    """Gemini's prioritization recommendation over already-computed,
    grounded insights.

    ADVISORY ONLY: this recommendation never changes any Issue, official
    department assignment, or status -- an authority must take official
    action separately through the existing lifecycle/assignment
    endpoints. recommended_priority_order is sanitized server-side (see
    backend.service.get_prioritized_insights) to only ever contain
    insight_type values CivicSync actually computed, regardless of what
    Gemini returns.
    """

    summary: str
    recommended_priority_order: list[str]
    explanation: str
    ai_generated: bool = True


class PrioritizedInsightsResponse(BaseModel):
    """Response for POST /api/admin/insights/prioritize.

    `insights` is the same grounded, deterministic list GET
    /api/admin/insights would return -- unchanged by the AI step.
    `ai_recommendation` is null if there were no insights to prioritize,
    or if Gemini was unavailable/failed; in that case
    `ai_recommendation_error` carries a sanitized (never raw-exception)
    explanation, and the grounded insights are still returned -- their
    value doesn't depend on Gemini succeeding.
    """

    insights: list[Insight]
    ai_recommendation: AIPrioritizationRecommendation | None
    ai_recommendation_error: str | None = None


# --- Jurisdiction hierarchy (Milestone 13) ------------------------------------


class JurisdictionSummary(BaseModel):
    """Minimal jurisdiction reference used inside an ancestry chain --
    excludes the internal integer id, same convention as DepartmentSummary."""

    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    level: JurisdictionLevel


class JurisdictionResponse(BaseModel):
    """Public API representation of a single jurisdiction.

    `ancestry` is the full root-to-leaf chain (e.g. India -> West Bengal
    -> Nadia -> Krishnanagar Municipality), included directly so a caller
    never needs a second request per jurisdiction to build a breadcrumb --
    see backend.repository.get_jurisdiction_ancestry, which computes this
    in one query regardless of hierarchy depth. Excludes the internal
    integer id and parent_jurisdiction_id; `parent_code` is the public
    equivalent.
    """

    code: str
    name: str
    level: JurisdictionLevel
    country_code: str
    parent_code: str | None
    is_active: bool
    ancestry: list[JurisdictionSummary]


class JurisdictionListResponse(BaseModel):
    jurisdictions: list[JurisdictionResponse]


# --- On-demand AI issue explanation (Milestone 14, Priority 2) ---------------


class IssueExplanationResponse(BaseModel):
    """Response for POST /api/admin/issues/{public_id}/explain.

    Never persisted -- generated fresh on every request from the issue's
    already-extracted structured fields, and discarded once returned.
    `explanation`/`considerations` are null if Gemini was unavailable or
    failed; in that case `error` carries a sanitized (never raw-exception)
    message. This endpoint never changes the issue in any way regardless
    of outcome -- see backend/service.py's get_issue_ai_explanation.
    """

    explanation: str | None
    considerations: list[str]
    error: str | None = None


# --- AI Operational Briefing (Milestone 15) -----------------------------------


class OperationalBriefingResponse(BaseModel):
    """Response for POST /api/admin/analytics/operational-briefing.

    Never persisted -- generated fresh on every request from the
    jurisdiction-scoped (and optionally department-scoped) set of
    currently active issues, and discarded once returned.
    `briefing`/`key_observations`/`priority_signals`/`considerations` are
    empty/null if Gemini was unavailable, failed, or there was nothing in
    scope to brief on; in that case `error` carries a sanitized (never
    raw-exception) message. This endpoint never changes any issue in any
    way regardless of outcome -- see
    backend/service.py's get_operational_briefing.
    """

    briefing: str | None
    key_observations: list[str]
    priority_signals: list[str]
    considerations: list[str]
    error: str | None = None
