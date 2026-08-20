"""
SQLAlchemy ORM models for CivicSync's persistence layer.

Issue is the durable, database-backed representation of a civic issue.
It is deliberately distinct from ai.schemas.CivicIssue: CivicIssue is the
AI's structured *output* for a single request (transient); Issue is what
actually gets stored and tracked over its operational lifecycle.

category and severity reuse the AI layer's own enums (IssueCategory,
SeverityLevel from ai.schemas) as the single source of truth for those
vocabularies, rather than redefining them here.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from ai.schemas import IssueCategory, SeverityLevel
from backend.database import Base


class IssueStatus(str, enum.Enum):
    """Official operational lifecycle of an issue.

    This is independent of anything the AI produces -- AI output never
    moves an issue between these states automatically.
    """

    SUBMITTED = "SUBMITTED"
    CLASSIFIED = "CLASSIFIED"
    ROUTED = "ROUTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    REOPENED = "REOPENED"


def generate_public_id() -> str:
    """Generate a unique, public-facing identifier for a new issue.

    v1 uses a random UUID4-derived string (e.g. "CIV-9F3A2C1B4E7D"). This
    is unique and stable, but is intentionally NOT the sequential
    "CIV-2026-000001" format described for a future iteration -- that
    format needs a coordinated, gap-free counter (e.g. a per-year
    sequence table or DB sequence), which is out of scope for this initial
    persistence layer and can replace this function later without
    affecting anything that only depends on public_id being unique.
    """
    return f"CIV-{uuid.uuid4().hex[:12].upper()}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Issue(Base):
    """A civic issue as tracked by CivicSync, from submission to closure."""

    __tablename__ = "issues"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Public-facing identifier. Deliberately separate from the integer
    # primary key so the internal DB id is never exposed to citizens or
    # policymakers.
    public_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False, default=generate_public_id
    )

    # --- AI-derived extraction (mirrors ai.schemas.CivicIssue) -----------
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[IssueCategory] = mapped_column(
        SAEnum(
            IssueCategory,
            name="issue_category",
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
    )
    problem: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    duration: Mapped[str | None] = mapped_column(String(255), nullable=True)
    severity: Mapped[SeverityLevel] = mapped_column(
        SAEnum(
            SeverityLevel,
            name="issue_severity",
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
        default=SeverityLevel.UNKNOWN,
    )
    affected_population: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # AI recommendation only -- never treated as an authoritative routing
    # decision. See assigned_department below for the official field.
    suggested_department: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    # --- Official / operational data (never set from AI output) ----------
    # The department actually assigned by staff. Independent of, and not
    # derived from, suggested_department.
    assigned_department: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[IssueStatus] = mapped_column(
        SAEnum(
            IssueStatus,
            name="issue_status",
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
        default=IssueStatus.SUBMITTED,
    )
    resolution_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Timestamps --------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    # Nullable until the issue actually reaches RESOLVED / CLOSED. Setting
    # these is an explicit operational action, never automatic based on AI
    # output.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return f"<Issue public_id={self.public_id!r} status={self.status!r}>"
