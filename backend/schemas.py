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
    assigned_department: str | None
    status: IssueStatus
    resolution_summary: str | None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    closed_at: datetime | None
