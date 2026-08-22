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

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

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


# Regex for CivicSync's current public_id format: "CIV-" + 12 uppercase hex
# characters (see generate_public_id() below). Defined once here so any
# route needing to validate a public_id path parameter (e.g. the public
# tracking endpoint) reuses this instead of duplicating the pattern.
PUBLIC_ID_PATTERN = r"^CIV-[0-9A-F]{12}$"


def generate_public_id() -> str:
    """Generate a unique, public-facing identifier for a new issue.

    v1 uses a random UUID4-derived string (e.g. "CIV-9F3A2C1B4E7D"). This
    is unique and stable, but is intentionally NOT the sequential
    "CIV-2026-000001" format described for a future iteration -- that
    format needs a coordinated, gap-free counter (e.g. a per-year
    sequence table or DB sequence), which is out of scope for this initial
    persistence layer and can replace this function later without
    affecting anything that only depends on public_id being unique.
    Must stay in sync with PUBLIC_ID_PATTERN above.
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

    # --- Jurisdiction scope vs. operational responsibility (Milestone 17) --
    #
    # These two fields answer two INDEPENDENT questions and must never be
    # derived from one another:
    #
    #   jurisdiction_id        "Where does this issue belong?"
    #                          The administrative/geographic scope (see
    #                          Jurisdiction above). Set once, at creation,
    #                          from the configured default jurisdiction
    #                          (see backend.repository.get_default_jurisdiction_id)
    #                          -- never derived from assigned_department,
    #                          and never from Gemini's free-text `location`
    #                          field, which is not authoritative geography.
    #
    #   assigned_department_id "Which department is officially responsible?"
    #                          Set later by an authority action (see
    #                          assign_department_to_issue), independent of
    #                          jurisdiction. NULL until an authority
    #                          assigns it -- this is normal, expected state
    #                          for SUBMITTED/CLASSIFIED issues, not a bug.
    #
    # An issue is therefore jurisdiction-scoped from the moment it's
    # created, while remaining unassigned (assigned_department_id IS NULL)
    # until an authority routes it -- both states are valid simultaneously.
    # Prior to this milestone, jurisdiction was derived transitively via
    # Issue -> assigned_department -> Department -> Jurisdiction, which
    # caused every SUBMITTED/CLASSIFIED issue (assigned_department_id IS
    # NULL by design) to silently disappear from jurisdiction-scoped
    # authority views. jurisdiction_id fixes this by making jurisdiction
    # scope a direct, always-present property of the Issue itself.
    jurisdiction_id: Mapped[int] = mapped_column(
        ForeignKey("jurisdictions.id"), nullable=False, index=True
    )
    jurisdiction: Mapped["Jurisdiction"] = relationship()

    # --- Official / operational data (never set from AI output) ----------
    # The department officially assigned by staff -- a real Department
    # entity from the controlled registry (see Department below), NOT
    # free text. Independent of, and never derived from,
    # suggested_department above. Nullable: an issue may have no official
    # assignment yet -- see the jurisdiction_id block above for why this
    # is expected, valid state rather than something to work around.
    assigned_department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id"), nullable=True
    )
    assigned_department: Mapped["Department | None"] = relationship()
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
    # The authenticated authority identity (the session username -- see
    # backend/auth.py's get_current_authority) that resolved this issue,
    # via POST /api/issues/{public_id}/resolve (Milestone 18, Phase 2).
    # Nullable: unset until an actual resolution happens, and stays NULL
    # for issues that were never resolved through that endpoint. Never
    # set by AI, and never accepted from request input -- always the
    # server-side authenticated identity.
    resolved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Append-only audit trail of status transitions, most recent last.
    # See IssueStatusHistory below.
    status_history: Mapped[list["IssueStatusHistory"]] = relationship(
        back_populates="issue",
        order_by="IssueStatusHistory.changed_at",
        cascade="all, delete-orphan",
    )
    # Resolution evidence (Milestone 18, Phase 3) -- zero or more
    # authority-uploaded files (photos, primarily) documenting the actual
    # work performed. Independent of the resolution note/resolved_by
    # above: evidence can be attached at any point while an authority is
    # working an issue, not only at the moment of resolution -- see
    # ResolutionEvidence below.
    evidence: Mapped[list["ResolutionEvidence"]] = relationship(
        back_populates="issue",
        order_by="ResolutionEvidence.uploaded_at",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return f"<Issue public_id={self.public_id!r} status={self.status!r}>"


EVIDENCE_PUBLIC_ID_PATTERN = r"^EVD-[0-9A-F]{12}$"


def generate_evidence_public_id() -> str:
    """A stable, non-sequential public reference for a ResolutionEvidence
    row -- mirrors generate_public_id() above exactly (same shape, "EVD-"
    prefix instead of "CIV-") so evidence references follow the same
    "never expose the internal integer id" convention as every other
    public-facing identifier in this project.
    """
    return f"EVD-{uuid.uuid4().hex[:12].upper()}"


class ResolutionEvidence(Base):
    """One authority-uploaded evidence file (a photo, primarily)
    documenting the resolution of an Issue (Milestone 18, Phase 3).

    Deliberately minimal metadata table, matching the storage boundary in
    backend/evidence_storage.py: this row records WHAT was uploaded and
    WHO/WHEN, never HOW the bytes are physically stored beyond the opaque
    `storage_key` (a server-generated identifier, never a filesystem path
    and never derived from the client-supplied filename -- see
    backend/service.py's upload_issue_evidence for why that matters for
    path-traversal safety).

    `original_filename` is a sanitized DISPLAY name only -- it is never
    used to construct a filesystem path or to look up the stored file.
    """

    __tablename__ = "resolution_evidence"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(
        String(20), unique=True, index=True, nullable=False, default=generate_evidence_public_id
    )
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id"), nullable=False, index=True)

    # Server-generated (secrets.token_hex-based), never client input --
    # the only thing ever passed to backend.evidence_storage.save()/load().
    storage_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # Sanitized DISPLAY name only (see class docstring) -- never used for
    # filesystem access.
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # Validated against a strict allowlist before this row is ever
    # created -- see backend/service.py's ALLOWED_EVIDENCE_CONTENT_TYPES.
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    # The authenticated authority identity that uploaded this file (see
    # backend/auth.py's get_current_authority) -- never accepted from
    # request input.
    uploaded_by: Mapped[str] = mapped_column(String(255), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    issue: Mapped["Issue"] = relationship(back_populates="evidence")

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return f"<ResolutionEvidence public_id={self.public_id!r} issue_id={self.issue_id!r}>"


class IssueStatusHistory(Base):
    """Append-only audit trail of Issue.status transitions.

    One row per transition, including the initial null -> SUBMITTED row
    written alongside a new Issue (see backend/repository.py). Rows are
    never updated or deleted -- this is a historical record, not
    operational state.
    """

    __tablename__ = "issue_status_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    issue_id: Mapped[int] = mapped_column(
        ForeignKey("issues.id"), nullable=False, index=True
    )

    # Null only for the very first history row (initial submission), which
    # has no prior status to record.
    from_status: Mapped[IssueStatus | None] = mapped_column(
        SAEnum(
            IssueStatus,
            name="issue_status_history_from_status",
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=True,
    )
    to_status: Mapped[IssueStatus] = mapped_column(
        SAEnum(
            IssueStatus,
            name="issue_status_history_to_status",
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    issue: Mapped["Issue"] = relationship(back_populates="status_history")

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return (
            f"<IssueStatusHistory issue_id={self.issue_id!r} "
            f"{self.from_status!r} -> {self.to_status!r}>"
        )


class JurisdictionLevel(str, enum.Enum):
    """The administrative hierarchy CivicSync's jurisdiction model
    represents: Country -> State/Province/Region -> District/Administrative
    Region -> Local Government/Municipality. Deliberately generic (not
    "IndianState"/"IndianDistrict") so the same four levels can represent
    any country's administrative structure -- see Jurisdiction's docstring
    for the India example and the BRICS-generalization rationale.
    """

    COUNTRY = "COUNTRY"
    STATE = "STATE"
    DISTRICT = "DISTRICT"
    LOCAL_BODY = "LOCAL_BODY"


class Jurisdiction(Base):
    """A node in CivicSync's administrative jurisdiction hierarchy.

    Self-referential: each row optionally points to a parent row one level
    up, so the same table represents an arbitrarily deep chain without a
    separate table per level. For India this looks like:

        India (COUNTRY, country_code="IN")
          -> West Bengal (STATE)
            -> Nadia (DISTRICT)
              -> Krishnanagar Municipality (LOCAL_BODY)

    The same four levels and the same `country_code` field work
    identically for any other country (Brazil, Russia, China, South
    Africa, ...) -- nothing here is India-specific in structure, only the
    seeded demo *data* is. This is deliberately a small, generic
    abstraction: four levels, a parent pointer, a country code. It is not
    a GIS system and not a national government registry.

    `Department.jurisdiction_id` (nullable) is the only place this
    connects to the rest of CivicSync today -- a department optionally
    belongs to a jurisdiction, and an Issue is scoped to a jurisdiction
    transitively through its assigned department. Issue itself is
    untouched by this milestone.
    """

    __tablename__ = "jurisdictions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Stable, human-meaningful public reference (e.g. "IN-WB-NADIA-KRISHNANAGAR").
    # Never the internal integer id -- consistent with how Department.code
    # already works.
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    level: Mapped[JurisdictionLevel] = mapped_column(
        SAEnum(
            JurisdictionLevel,
            name="jurisdiction_level",
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
    )
    # ISO-3166-alpha-2-style country code, present on every row regardless
    # of level (not just COUNTRY rows) so a query can filter "everything
    # in India" without walking the tree. This is the concrete field that
    # makes BRICS generalization real rather than aspirational: switching
    # country_code is what it takes to stand up a jurisdiction tree for a
    # different country.
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, index=True)

    parent_jurisdiction_id: Mapped[int | None] = mapped_column(
        ForeignKey("jurisdictions.id"), nullable=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    parent: Mapped["Jurisdiction | None"] = relationship(remote_side=[id])

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return f"<Jurisdiction code={self.code!r} level={self.level!r}>"


class Department(Base):
    """A civic department in CivicSync's controlled registry.

    This is the single source of truth for department identity -- an
    Issue's official assigned_department is always a foreign key into
    this table, never an arbitrary string (that's what
    Issue.suggested_department, the AI's free-text recommendation, is
    for). The registry is seeded once via Alembic data migration and is
    not (yet) end-user-creatable.
    """

    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Nullable: a department belonging to no specific jurisdiction (e.g.
    # NULL) remains valid -- this is what makes the M13 jurisdiction
    # hierarchy purely additive. Every department that existed before this
    # migration keeps working with jurisdiction_id=NULL until/unless a
    # migration explicitly backfills it (see the M13 migration, which
    # backfills the existing seed departments to a real demo jurisdiction).
    jurisdiction_id: Mapped[int | None] = mapped_column(
        ForeignKey("jurisdictions.id"), nullable=True, index=True
    )
    jurisdiction: Mapped["Jurisdiction | None"] = relationship()

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return f"<Department code={self.code!r} name={self.name!r}>"


class IssueAssignmentHistory(Base):
    """Append-only audit trail of official department assignments.

    One row per assignment. Reassigning an issue closes the previous
    active row (unassigned_at set) and opens a new one -- rows are never
    deleted, so the full assignment history is always reconstructable.
    """

    __tablename__ = "issue_assignment_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id"), nullable=False, index=True)
    department_id: Mapped[int] = mapped_column(
        ForeignKey("departments.id"), nullable=False, index=True
    )

    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    # Null while this is the active assignment; set when the issue is
    # reassigned to a different department.
    unassigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    issue: Mapped["Issue"] = relationship()
    department: Mapped["Department"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return (
            f"<IssueAssignmentHistory issue_id={self.issue_id!r} "
            f"department_id={self.department_id!r} unassigned_at={self.unassigned_at!r}>"
        )


REOPEN_REQUEST_PUBLIC_ID_PATTERN = r"^RRQ-[0-9A-F]{12}$"


def generate_reopen_request_public_id() -> str:
    """Mirrors generate_public_id()/generate_evidence_public_id() exactly
    (same shape, "RRQ-" prefix) -- same "never expose the internal
    integer id" convention as every other public-facing identifier in
    this project."""
    return f"RRQ-{uuid.uuid4().hex[:12].upper()}"


class ReopenRequestState(str, enum.Enum):
    """Citizen reopen request review state (Milestone 18, Phase 5).

    Deliberately separate from IssueStatus -- a reopen request is a
    REQUEST for authority action, not a status change itself. Only an
    authority decision (via the existing lifecycle transition mechanism)
    ever actually moves the Issue between RESOLVED and REOPENED; this
    enum only tracks the request's own review lifecycle.
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ReopenRequest(Base):
    """A citizen's request to reopen a RESOLVED issue (Milestone 18,
    Phase 5).

    Intentionally minimal and independent of Issue's own lifecycle
    fields -- this table records the REQUEST and its review, never the
    actual status change (that's still recorded exactly once, in
    IssueStatusHistory, via the existing transition mechanism -- see
    backend/repository.py's decide_reopen_request). At most one PENDING
    request may exist per issue at a time (enforced in
    backend/service.py's request_issue_reopen, not by a database
    constraint, matching the project's existing convention of enforcing
    business rules in the service layer rather than the schema).
    """

    __tablename__ = "reopen_requests"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(
        String(20), unique=True, index=True, nullable=False, default=generate_reopen_request_public_id
    )
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id"), nullable=False, index=True)

    citizen_reason: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[ReopenRequestState] = mapped_column(
        SAEnum(ReopenRequestState, native_enum=False, length=20),
        nullable=False,
        default=ReopenRequestState.PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    # Populated only once an authority decides -- all three null while
    # PENDING. decided_by is the authenticated authority identity (see
    # backend/auth.py's get_current_authority) -- never accepted from
    # request input, and never exposed in any API response (same
    # authority-only treatment as Issue.resolved_by -- see Milestone 18
    # Phase 4's ReopenRequestResponse, which omits it).
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    issue: Mapped["Issue"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return f"<ReopenRequest public_id={self.public_id!r} state={self.state!r}>"
