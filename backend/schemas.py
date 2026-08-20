"""
API request/response models for the FastAPI layer.

Kept separate from ai/schemas.py: these describe the HTTP contract for
backend/main.py, while ai/schemas.py describes the AI service's own data
model (CivicIssue). CivicIssue is reused directly as the response model
for POST /api/analyze (AI-only, not persisted).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ai.schemas import IssueCategory, SeverityLevel
from backend.models import IssueStatus


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
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    closed_at: datetime | None


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


class StatusHistoryEntryResponse(BaseModel):
    """Public API representation of a single status-history row.

    Excludes the row's own id and issue_id -- callers only ever look up
    history by an Issue's public_id, never by these internal ids.
    """

    model_config = ConfigDict(from_attributes=True)

    from_status: IssueStatus | None
    to_status: IssueStatus
    reason: str | None
    changed_at: datetime


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
    assigned_at: datetime
    reason: str | None


class AssignmentHistoryEntryResponse(BaseModel):
    """Public API representation of a single assignment-history row.

    Excludes the row's own id, issue_id, and department_id -- department
    identity is exposed via department_code/department_name instead.
    """

    department_code: str
    department_name: str
    assigned_at: datetime
    unassigned_at: datetime | None
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
    timestamp: datetime
    reason: str | None


class PublicIssueTrackingResponse(BaseModel):
    """Public, citizen-safe tracking representation for
    GET /api/track/{public_id}.

    Deliberately a distinct model from IssueResponse, not a trimmed-down
    reuse of it: IssueResponse is the internal/admin representation and
    includes original_text, confidence, suggested_department, and
    resolution_summary -- none of which belong on an unauthenticated
    public endpoint. This model is always built explicitly (see
    backend.service.get_public_tracking), never populated directly from
    an Issue ORM instance via from_attributes, so a future addition to
    Issue can never silently leak through here.
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
    created_at: datetime
    updated_at: datetime
    timeline: list[PublicTimelineEntry]
