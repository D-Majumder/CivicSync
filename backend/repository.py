"""
Service-layer functions for persisting and retrieving Issue records.

This is intentionally small -- a full CRUD API is a later task. It exists
so persistence logic has one home (not duplicated across tests or, later,
routes), consistent with CivicSync's API -> Service -> Database layering.
"""

from __future__ import annotations

import os
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
    Jurisdiction,
    JurisdictionLevel,
    ReopenRequest,
    ReopenRequestState,
    ResolutionEvidence,
)
from backend.transitions import validate_transition

# Terminal lifecycle states -- an issue in one of these is done, in either
# direction (successfully closed, or rejected as not actionable). Governs
# the operational queue's exclusion list and the aging-backlog view below:
# both deliberately keep showing a RESOLVED-but-not-yet-CLOSED issue,
# since it may still need authority attention (e.g. a pending citizen
# reopen request).
TERMINAL_STATUSES: frozenset[IssueStatus] = frozenset({IssueStatus.CLOSED, IssueStatus.REJECTED})

# Statuses that represent work still requiring operational attention --
# everything except the terminal states above.
OPERATIONAL_STATUSES: frozenset[IssueStatus] = frozenset(
    s for s in IssueStatus if s not in TERMINAL_STATUSES
)

# Statuses treated as "inactive" for authority-facing REPORTING metrics
# (Command Center KPIs, the Departments page, and Civic Intelligence
# advisories) -- deliberately broader than TERMINAL_STATUSES above. A
# RESOLVED issue is operationally done from a metrics standpoint (it
# shouldn't inflate an "active issues" count) even though it still needs
# to remain visible in the operational queue/aging views until formally
# CLOSED. This is the single shared definition every "active issue" count
# below -- and every caller in backend/service.py -- must use; do not
# reintroduce a local copy of this set anywhere else.
METRIC_INACTIVE_STATUSES: frozenset[IssueStatus] = TERMINAL_STATUSES | {IssueStatus.RESOLVED}

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


def get_default_jurisdiction_id(db: Session) -> int:
    """Resolve the configured default jurisdiction (Milestone 17) for
    newly-created issues, from the CIVICSYNC_DEFAULT_JURISDICTION_CODE
    environment variable.

    This is a temporary MVP mechanism -- CivicSync has no per-citizen
    geographic/location resolution yet, so every new issue is scoped to
    one operator-configured jurisdiction. It deliberately does NOT use
    Gemini's free-text `location` field (not authoritative geography) or
    suggested_department (an AI recommendation, not a jurisdiction
    signal) to determine this.

    Fails loudly (raises EnvironmentError) rather than silently leaving
    jurisdiction_id NULL if the environment variable is unset, or if it
    doesn't match any real, active Jurisdiction -- a misconfigured
    deployment must not silently create ungoverned data.
    """
    code = os.environ.get("CIVICSYNC_DEFAULT_JURISDICTION_CODE")
    if not code:
        raise EnvironmentError(
            "CIVICSYNC_DEFAULT_JURISDICTION_CODE is not set. Every new issue "
            "must be scoped to a jurisdiction -- add this to your .env file "
            "(see .env.example)."
        )
    jurisdiction = db.query(Jurisdiction).filter(Jurisdiction.code == code).one_or_none()
    if jurisdiction is None or not jurisdiction.is_active:
        raise EnvironmentError(
            f"CIVICSYNC_DEFAULT_JURISDICTION_CODE is set to {code!r}, but no "
            "active jurisdiction with that code exists. Check the seeded "
            "jurisdiction data and this environment variable."
        )
    return jurisdiction.id


def create_issue_from_civic_issue(
    db: Session,
    civic_issue: CivicIssue,
    jurisdiction_id: int,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    location_accuracy: float | None = None,
    citizen_language: str | None = None,
) -> Issue:
    """Persist a new Issue from a freshly AI-analyzed CivicIssue.

    jurisdiction_id is REQUIRED (Milestone 17) -- see
    get_default_jurisdiction_id above for how a caller resolves it, and
    Issue's docstring in backend/models.py for why jurisdiction and
    assigned_department are independent, both-required-eventually, but
    never-derived-from-each-other fields.

    latitude/longitude/location_accuracy (Milestone 21) and
    citizen_language (Milestone 22) are entirely OPTIONAL, keyword-only,
    and default to None -- every existing caller of this function (and
    every existing test) continues to work unchanged. These come from
    the CITIZEN's device/UI selection, NOT from Gemini -- deliberately
    kept as separate parameters rather than folded into CivicIssue,
    preserving the same citizen-data-vs-AI-extracted-data separation
    already established throughout this project.

    Only fields CivicIssue actually produces are populated here. Official/
    operational fields (assigned_department_id, resolution_summary,
    resolved_at, closed_at) are left at their defaults -- AI output is
    extraction, not an operational decision, so it never sets those, and
    this function does not auto-assign a department based on jurisdiction
    either.

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
        jurisdiction_id=jurisdiction_id,
        latitude=latitude,
        longitude=longitude,
        location_accuracy=location_accuracy,
        citizen_language=citizen_language,
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


def resolve_issue(db: Session, issue: Issue, resolution_note: str, resolved_by: str) -> Issue:
    """Apply the authority resolution workflow (Milestone 18, Phase 2):
    record who resolved the issue and how, then transition it to
    RESOLVED -- as ONE atomic operation.

    Reuses _apply_validated_status_transition (the exact same primitive
    transition_issue_status above uses) to perform the actual status
    change, so IN_PROGRESS -> RESOLVED legality is enforced by the same
    single authoritative validator as every other transition in the
    system -- this function does not duplicate or bypass that check.

    Order matters for atomicity: validate_transition() is checked FIRST,
    before resolution_summary/resolved_by are touched at all, so a call
    against an issue that isn't currently IN_PROGRESS raises
    InvalidTransitionError with NOTHING mutated on the Issue object --
    not even in memory -- and nothing committed. (autoflush is disabled
    on this project's sessions -- see backend/database.py -- so even if
    order were reversed, nothing would reach the database before an
    explicit commit; checking first is an extra, deliberate safety
    margin against that assumption ever changing.)

    Raises InvalidTransitionError if the issue is not currently
    IN_PROGRESS. Raises SQLAlchemyError on persistence failure; the
    caller (service layer) is responsible for rolling back the session.
    """
    validate_transition(issue.status, IssueStatus.RESOLVED)

    issue.resolution_summary = resolution_note
    issue.resolved_by = resolved_by
    _apply_validated_status_transition(db, issue, IssueStatus.RESOLVED, reason=resolution_note)
    db.commit()
    db.refresh(issue)
    return issue


# --- Resolution evidence (Milestone 18, Phase 3) -----------------------------


def create_evidence(
    db: Session,
    issue: Issue,
    *,
    storage_key: str,
    original_filename: str,
    content_type: str,
    size_bytes: int,
    uploaded_by: str,
) -> ResolutionEvidence:
    """Persist one evidence row, committed on its own (independent of the
    resolution note/status -- evidence upload is not itself a lifecycle
    transition, see backend/service.py's upload_issue_evidence for the
    full atomicity story, which saves the file BEFORE this call and
    rolls back the saved file if this fails).
    """
    evidence = ResolutionEvidence(
        issue_id=issue.id,
        storage_key=storage_key,
        original_filename=original_filename,
        content_type=content_type,
        size_bytes=size_bytes,
        uploaded_by=uploaded_by,
    )
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return evidence


def get_evidence_by_public_id(db: Session, public_id: str) -> ResolutionEvidence | None:
    """Fetch a single ResolutionEvidence row by its public reference.
    Never accepts or resolves a raw storage_key/path from a caller --
    this is the only lookup path evidence retrieval uses."""
    return (
        db.query(ResolutionEvidence)
        .filter(ResolutionEvidence.public_id == public_id)
        .one_or_none()
    )


def get_evidence_for_issue(db: Session, issue: Issue) -> list[ResolutionEvidence]:
    """All evidence for an issue, oldest upload first."""
    return (
        db.query(ResolutionEvidence)
        .filter(ResolutionEvidence.issue_id == issue.id)
        .order_by(ResolutionEvidence.uploaded_at.asc())
        .all()
    )


# --- Citizen reopen requests (Milestone 18, Phase 5) --------------------------


def get_pending_reopen_request(db: Session, issue: Issue) -> ReopenRequest | None:
    """The issue's currently PENDING reopen request, if any -- at most
    one may exist per issue (enforced in backend/service.py's
    request_issue_reopen, which calls this to check before creating a
    new one)."""
    return (
        db.query(ReopenRequest)
        .filter(
            ReopenRequest.issue_id == issue.id,
            ReopenRequest.state == ReopenRequestState.PENDING,
        )
        .one_or_none()
    )


def get_issue_ids_with_pending_reopen_request(
    db: Session, issue_ids: list[int]
) -> set[int]:
    """Bulk version of get_pending_reopen_request, for list/table views
    (Issue Queue, All Issues, Stale Issues) that need to flag many issues
    at once without one query per row. Returns the subset of issue_ids
    that currently have a PENDING reopen request. Empty input -> empty
    result, no query issued."""
    if not issue_ids:
        return set()
    rows = (
        db.query(ReopenRequest.issue_id)
        .filter(
            ReopenRequest.issue_id.in_(issue_ids),
            ReopenRequest.state == ReopenRequestState.PENDING,
        )
        .distinct()
        .all()
    )
    return {row[0] for row in rows}


def get_latest_reopen_request_states(
    db: Session, issue_ids: list[int]
) -> dict[int, ReopenRequestState]:
    """Bulk lookup of each issue's MOST RECENT reopen request state
    (PENDING, APPROVED, or REJECTED), regardless of state -- unlike
    get_issue_ids_with_pending_reopen_request above, which only reports
    currently-pending ones.

    Used to drive the authority-facing "current reopen status" badge,
    which must reflect only the latest request when a citizen has
    submitted more than one over an issue's lifetime (e.g. rejected, then
    a later one approved) -- never a stale earlier state. Issues with no
    reopen request at all are simply absent from the returned dict.

    request_issue_reopen (backend/service.py) only ever allows a new
    request to be created once any prior one has been decided, so the
    most-recently-created request (highest id) is always the current one
    to reflect -- this is a single bulk SQL query, not one per issue, and
    the reduction to "most recent per issue" happens in Python over that
    one result set. Empty input -> empty result, no query issued.
    """
    if not issue_ids:
        return {}
    rows = (
        db.query(ReopenRequest.issue_id, ReopenRequest.state)
        .filter(ReopenRequest.issue_id.in_(issue_ids))
        .order_by(ReopenRequest.issue_id, ReopenRequest.id.desc())
        .all()
    )
    latest_state_by_issue_id: dict[int, ReopenRequestState] = {}
    for issue_id, state in rows:
        # Rows are ordered id DESC within each issue_id, so the first
        # occurrence seen for a given issue_id is its latest request.
        if issue_id not in latest_state_by_issue_id:
            latest_state_by_issue_id[issue_id] = state
    return latest_state_by_issue_id


def get_reopen_request_by_public_id(db: Session, public_id: str) -> ReopenRequest | None:
    """Fetch a single ReopenRequest by its public reference. Never
    accepts or resolves a raw internal id from a caller."""
    return db.query(ReopenRequest).filter(ReopenRequest.public_id == public_id).one_or_none()


def create_reopen_request(db: Session, issue: Issue, citizen_reason: str) -> ReopenRequest:
    """Persist a new PENDING reopen request. Does NOT touch Issue.status
    at all -- a request is only ever a request; see
    decide_reopen_request below for the actual (authority-gated) status
    change on approval."""
    request = ReopenRequest(issue_id=issue.id, citizen_reason=citizen_reason)
    db.add(request)
    db.commit()
    db.refresh(request)
    return request


def decide_reopen_request(
    db: Session,
    request: ReopenRequest,
    issue: Issue,
    *,
    approve: bool,
    decision_reason: str | None,
    decided_by: str,
) -> ReopenRequest:
    """Apply an authority's decision on a reopen request, as one atomic
    operation covering the request's own decision fields and, on
    approval, the Issue status transition plus its history entry.

    On approval: reuses _apply_validated_status_transition (the same
    primitive transition_issue_status/resolve_issue use) to move the
    issue from RESOLVED to REOPENED -- an existing, already-valid
    transition (see backend/transitions.py), not a parallel status
    system. Validated first, before any mutation, so a request against
    an issue no longer RESOLVED (e.g. closed through another path since
    the request was filed) raises InvalidTransitionError with nothing
    mutated, not even the request's own state.

    On rejection: only the request's state becomes REJECTED -- the issue
    remains RESOLVED, no history entry is created. resolution_summary/
    resolved_by on the Issue are never cleared in either outcome,
    preserving the original resolution for auditability.

    Raises InvalidTransitionError (approval only) if the issue isn't
    currently RESOLVED. Raises SQLAlchemyError on persistence failure;
    the caller is responsible for rolling back the session.
    """
    if approve:
        # Checked first, before touching the request at all -- see
        # resolve_issue's identical ordering rationale above.
        validate_transition(issue.status, IssueStatus.REOPENED)
        request.state = ReopenRequestState.APPROVED
        request.decision_reason = decision_reason
        request.decided_at = datetime.now(timezone.utc)
        request.decided_by = decided_by
        _apply_validated_status_transition(
            db, issue, IssueStatus.REOPENED, reason=decision_reason or request.citizen_reason
        )
    else:
        request.state = ReopenRequestState.REJECTED
        request.decision_reason = decision_reason
        request.decided_at = datetime.now(timezone.utc)
        request.decided_by = decided_by

    db.commit()
    db.refresh(request)
    return request


# --- Resolution & reopening operational intelligence (Milestone 19) ----------


def get_resolution_intelligence(
    db: Session, *, jurisdiction_code: str | None = None
) -> dict | None:
    """Compute every resolution/reopening/evidence-coverage KPI in one
    pass (Milestone 19).

    jurisdiction_code scopes to that jurisdiction's subtree via
    Issue.jurisdiction_id, the same primitive every other jurisdiction
    -aware analytics function in this module uses. Returns None if the
    code doesn't match any real jurisdiction (caller maps that to 404).

    "Total resolved" counts issues that have EVER been resolved
    (resolved_at IS NOT NULL) -- this correctly includes an issue that
    was later REOPENED (it still represents a completed resolution
    event, just one a citizen disputed), not only issues currently
    sitting in RESOLVED/CLOSED status.

    Median is computed in Python (SQLite has no native MEDIAN
    aggregate) over the same small set of resolved issues already
    needed for the evidence-coverage count below -- one query, not two.
    """
    subtree_ids = None
    if jurisdiction_code is not None:
        subtree_ids = get_jurisdiction_subtree_ids(db, jurisdiction_code)
        if subtree_ids is None:
            return None

    issues_query = db.query(Issue)
    if subtree_ids is not None:
        issues_query = issues_query.filter(Issue.jurisdiction_id.in_(subtree_ids))

    total_issues = issues_query.count()
    total_non_rejected = issues_query.filter(Issue.status != IssueStatus.REJECTED).count()
    currently_reopened = issues_query.filter(Issue.status == IssueStatus.REOPENED).count()

    resolved_issues = issues_query.filter(Issue.resolved_at.isnot(None)).all()
    total_resolved = len(resolved_issues)

    resolution_hours = sorted(
        (issue.resolved_at - issue.created_at).total_seconds() / 3600.0
        for issue in resolved_issues
    )
    avg_resolution_hours = (
        sum(resolution_hours) / len(resolution_hours) if resolution_hours else None
    )
    if resolution_hours:
        mid = len(resolution_hours) // 2
        if len(resolution_hours) % 2 == 1:
            median_resolution_hours = resolution_hours[mid]
        else:
            median_resolution_hours = (resolution_hours[mid - 1] + resolution_hours[mid]) / 2
    else:
        median_resolution_hours = None

    resolved_issue_ids = [issue.id for issue in resolved_issues]
    issue_ids_with_evidence: set[int] = set()
    if resolved_issue_ids:
        rows = (
            db.query(ResolutionEvidence.issue_id)
            .filter(ResolutionEvidence.issue_id.in_(resolved_issue_ids))
            .distinct()
            .all()
        )
        issue_ids_with_evidence = {row[0] for row in rows}
    resolutions_with_evidence = len(issue_ids_with_evidence)
    resolutions_without_evidence = total_resolved - resolutions_with_evidence

    reopen_requests_query = db.query(ReopenRequest).join(Issue, ReopenRequest.issue_id == Issue.id)
    if subtree_ids is not None:
        reopen_requests_query = reopen_requests_query.filter(Issue.jurisdiction_id.in_(subtree_ids))
    total_reopen_requests = reopen_requests_query.count()
    pending_reopen_requests = reopen_requests_query.filter(
        ReopenRequest.state == ReopenRequestState.PENDING
    ).count()
    approved_reopen_requests = reopen_requests_query.filter(
        ReopenRequest.state == ReopenRequestState.APPROVED
    ).count()
    rejected_reopen_requests = reopen_requests_query.filter(
        ReopenRequest.state == ReopenRequestState.REJECTED
    ).count()

    approved_by_category_rows = (
        db.query(Issue.category, func.count(ReopenRequest.id))
        .join(ReopenRequest, ReopenRequest.issue_id == Issue.id)
        .filter(ReopenRequest.state == ReopenRequestState.APPROVED)
    )
    if subtree_ids is not None:
        approved_by_category_rows = approved_by_category_rows.filter(
            Issue.jurisdiction_id.in_(subtree_ids)
        )
    approved_by_category = {
        category.value: count
        for category, count in approved_by_category_rows.group_by(Issue.category).all()
        if count > 0
    }

    return {
        "total_issues": total_issues,
        "total_non_rejected_issues": total_non_rejected,
        "total_resolved": total_resolved,
        "avg_resolution_hours": avg_resolution_hours,
        "median_resolution_hours": median_resolution_hours,
        "currently_reopened": currently_reopened,
        "total_reopen_requests": total_reopen_requests,
        "pending_reopen_requests": pending_reopen_requests,
        "approved_reopen_requests": approved_reopen_requests,
        "rejected_reopen_requests": rejected_reopen_requests,
        "resolutions_with_evidence": resolutions_with_evidence,
        "resolutions_without_evidence": resolutions_without_evidence,
        "approved_reopens_by_category": approved_by_category,
    }


# --- Geospatial civic intelligence (Milestone 20) -----------------------------


def get_geospatial_issues(
    db: Session, *, jurisdiction_code: str | None = None
) -> list[Issue] | None:
    """Active (non-terminal), geo-tagged issues within the last
    HOTSPOT_TIME_WINDOW_DAYS -- the raw input for both the geographic
    list view and hotspot detection (backend/hotspots.py).

    Only issues with BOTH latitude and longitude set are returned --
    never fabricated or geocoded. jurisdiction_code scopes to that
    jurisdiction's subtree via Issue.jurisdiction_id, the same primitive
    every other jurisdiction-aware analytics function in this module
    uses; returns None if the code doesn't match any real jurisdiction
    (caller maps that to 404).
    """
    from backend.hotspots import HOTSPOT_TIME_WINDOW_DAYS

    subtree_ids = None
    if jurisdiction_code is not None:
        subtree_ids = get_jurisdiction_subtree_ids(db, jurisdiction_code)
        if subtree_ids is None:
            return None

    cutoff = datetime.now(timezone.utc) - timedelta(days=HOTSPOT_TIME_WINDOW_DAYS)
    query = (
        db.query(Issue)
        .filter(Issue.status.in_(OPERATIONAL_STATUSES))
        .filter(Issue.latitude.isnot(None), Issue.longitude.isnot(None))
        .filter(Issue.created_at >= cutoff)
    )
    if subtree_ids is not None:
        query = query.filter(Issue.jurisdiction_id.in_(subtree_ids))
    return query.order_by(Issue.public_id.asc()).all()


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


def get_jurisdiction_subtree_ids(db: Session, jurisdiction_code: str) -> set[int] | None:
    """Resolve a jurisdiction code (at ANY level -- country, state,
    district, or local body) to the set of jurisdiction ids in its
    subtree: itself plus every descendant.

    Returns None if jurisdiction_code doesn't match any jurisdiction (the
    caller maps that to 404).

    This is THE single reusable primitive behind every jurisdiction-scoped
    query in this module (Milestones 14-17) -- filtering issues by
    "Nadia" (a DISTRICT) must include every LOCAL_BODY beneath it, not
    just rows directly tagged with Nadia's own id. Generic by
    construction -- works identically for any jurisdiction code at any
    level, not hardcoded to Krishnanagar. Every jurisdiction-scoped
    repository function below calls this ONCE rather than re-implementing
    the subtree walk.

    One query total regardless of hierarchy size or depth: the
    jurisdictions table is small by design (a handful of administrative
    levels, not a GIS dataset), so it's loaded once and walked in Python.
    """
    all_jurisdictions = db.query(Jurisdiction).all()
    root = next((j for j in all_jurisdictions if j.code == jurisdiction_code), None)
    if root is None:
        return None

    children_by_parent: dict[int, list[int]] = {}
    for j in all_jurisdictions:
        if j.parent_jurisdiction_id is not None:
            children_by_parent.setdefault(j.parent_jurisdiction_id, []).append(j.id)

    subtree_ids: set[int] = set()
    stack = [root.id]
    while stack:
        current_id = stack.pop()
        if current_id in subtree_ids:
            continue
        subtree_ids.add(current_id)
        stack.extend(children_by_parent.get(current_id, []))
    return subtree_ids


def get_jurisdiction_subtree_department_ids(
    db: Session, jurisdiction_code: str
) -> list[int] | None:
    """Resolve a jurisdiction code to the ids of every Department whose
    OWN jurisdiction (Department.jurisdiction_id, set once at department
    creation -- see Milestone 13) is that jurisdiction or a descendant of
    it. Used only where a query needs to filter/join through Department
    itself (e.g. department workload); for filtering Issue rows directly,
    use Issue.jurisdiction_id with get_jurisdiction_subtree_ids instead
    (Milestone 17) -- these are independent scopes, see Issue's docstring
    in backend/models.py.

    Returns None if jurisdiction_code doesn't match any jurisdiction (the
    caller maps that to 404) -- an empty list is a valid, different
    result (a real jurisdiction with zero departments yet).
    """
    subtree_ids = get_jurisdiction_subtree_ids(db, jurisdiction_code)
    if subtree_ids is None:
        return None

    department_ids = [
        d.id
        for d in db.query(Department.id).filter(Department.jurisdiction_id.in_(subtree_ids)).all()
    ]
    return department_ids


def get_active_issues_for_briefing(
    db: Session, *, jurisdiction_code: str, department_code: str | None = None
) -> list[Issue] | None:
    """Currently active (non-terminal) issues scoped to a jurisdiction's
    subtree, optionally further narrowed to a single department --
    Milestone 15's "AI Operational Briefing" data source.

    Reuses get_jurisdiction_subtree_department_ids for jurisdiction
    resolution (no jurisdiction logic duplicated) and the same
    OPERATIONAL_STATUSES / severity-priority ordering already used by
    get_operational_queue above. Returns None if jurisdiction_code
    doesn't match any real jurisdiction (caller maps that to 404).

    If department_code is given but doesn't belong to this jurisdiction's
    subtree, the combined filter naturally yields an empty list (not an
    error) -- exactly the same safe compose-by-AND behavior already
    established for the M14 list/queue/stale endpoints.
    """
    subtree_ids = get_jurisdiction_subtree_ids(db, jurisdiction_code)
    if subtree_ids is None:
        return None

    query = (
        db.query(Issue)
        .options(joinedload(Issue.assigned_department))
        .filter(Issue.jurisdiction_id.in_(subtree_ids))
        .filter(Issue.status.in_(OPERATIONAL_STATUSES))
    )
    if department_code is not None:
        query = query.filter(Issue.assigned_department.has(Department.code == department_code))

    return query.order_by(_SEVERITY_PRIORITY_CASE.asc(), Issue.updated_at.asc()).all()


def list_issues_for_admin(
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
) -> tuple[list[Issue], int] | None:
    """Filtered, sorted, paginated issue list for GET /api/admin/issues.

    Filters are ANDed together. department_code filters by the OFFICIAL
    assigned department (Issue.assigned_department), never
    suggested_department. jurisdiction_code filters to every department
    under that jurisdiction OR any of its descendants (see
    get_jurisdiction_subtree_department_ids) -- composable with
    department_code (both can narrow the same query at once). Returns
    (items, total) where total is the count matching the filters BEFORE
    limit/offset are applied, or None if jurisdiction_code doesn't match
    any real jurisdiction (caller maps that to 404).
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
    if jurisdiction_code is not None:
        subtree_ids = get_jurisdiction_subtree_ids(db, jurisdiction_code)
        if subtree_ids is None:
            return None
        query = query.filter(Issue.jurisdiction_id.in_(subtree_ids))

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


def count_issues_by_status(
    db: Session, *, jurisdiction_code: str | None = None
) -> dict[IssueStatus, int] | None:
    """Issue counts grouped by status, via SQL GROUP BY. Every IssueStatus
    is present in the result, zero-filled if no issues currently have it.
    jurisdiction_code scopes to that jurisdiction's subtree (see
    get_jurisdiction_subtree_department_ids); returns None if the code
    doesn't match any real jurisdiction (caller maps that to 404)."""
    counts: dict[IssueStatus, int] = {s: 0 for s in IssueStatus}
    query = db.query(Issue.status, func.count(Issue.id))
    if jurisdiction_code is not None:
        subtree_ids = get_jurisdiction_subtree_ids(db, jurisdiction_code)
        if subtree_ids is None:
            return None
        query = query.filter(Issue.jurisdiction_id.in_(subtree_ids))
    rows = query.group_by(Issue.status).all()
    for status_value, count in rows:
        counts[status_value] = count
    return counts


def count_issues_by_severity(
    db: Session, *, jurisdiction_code: str | None = None
) -> dict[SeverityLevel, int] | None:
    """Issue counts grouped by severity, via SQL GROUP BY. Every
    SeverityLevel is present in the result, zero-filled if unused.
    jurisdiction_code scopes to that jurisdiction's subtree; returns None
    if the code doesn't match any real jurisdiction."""
    counts: dict[SeverityLevel, int] = {s: 0 for s in SeverityLevel}
    query = db.query(Issue.severity, func.count(Issue.id))
    if jurisdiction_code is not None:
        subtree_ids = get_jurisdiction_subtree_ids(db, jurisdiction_code)
        if subtree_ids is None:
            return None
        query = query.filter(Issue.jurisdiction_id.in_(subtree_ids))
    rows = query.group_by(Issue.severity).all()
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
    active = sum(count for st, count in by_status.items() if st not in METRIC_INACTIVE_STATUSES)

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
    jurisdiction_code: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Issue], int] | None:
    """Issues requiring operational attention (excludes CLOSED, REJECTED),
    ordered by severity priority (CRITICAL first) then oldest updated_at
    first -- both computed in SQL, not Python. Returns (items, total), or
    None if jurisdiction_code doesn't match any real jurisdiction."""
    query = (
        db.query(Issue)
        .options(joinedload(Issue.assigned_department))
        .filter(Issue.status.in_(OPERATIONAL_STATUSES))
    )
    if department_code is not None:
        query = query.filter(Issue.assigned_department.has(Department.code == department_code))
    if severity is not None:
        query = query.filter(Issue.severity == severity)
    if jurisdiction_code is not None:
        subtree_ids = get_jurisdiction_subtree_ids(db, jurisdiction_code)
        if subtree_ids is None:
            return None
        query = query.filter(Issue.jurisdiction_id.in_(subtree_ids))

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
    jurisdiction_code: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Issue], int] | None:
    """Issues whose updated_at is at least older_than_hours old. Read-only
    -- never mutates anything. Returns (items, total), oldest-updated
    first, or None if jurisdiction_code doesn't match any real
    jurisdiction.

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
    if jurisdiction_code is not None:
        subtree_ids = get_jurisdiction_subtree_ids(db, jurisdiction_code)
        if subtree_ids is None:
            return None
        query = query.filter(Issue.jurisdiction_id.in_(subtree_ids))

    total = query.count()
    items = query.order_by(Issue.updated_at.asc()).limit(limit).offset(offset).all()
    return items, total


# --- Civic Intelligence & Analytics (Milestone 9) ----------------------------
#
# All read-only. All aggregation happens in SQL (GROUP BY / AVG / a single
# CASE-bucketed GROUP BY) -- never by pulling every Issue into Python and
# counting/averaging there. Reuses count_issues_by_status/severity from
# Milestone 8 rather than duplicating those queries (see backend/service.py).

# Aging is computed from julianday('now') - julianday(created_at) directly
# in SQL. Verified against this project's actual stored datetime format
# (naive UTC, matching how the rest of the app already writes timestamps --
# see get_stale_issues' docstring above for the same caveat) to produce
# correct day-granularity buckets. Boundaries are plain, neutral duration
# buckets -- not SLA targets; no bucket implies "on time" or "overdue".
_AGING_BUCKET_LABELS: tuple[str, ...] = (
    "0-1 day",
    "1-3 days",
    "3-7 days",
    "7-14 days",
    "14-30 days",
    "30+ days",
)


def _aging_bucket_expr():
    age_days = func.julianday("now") - func.julianday(Issue.created_at)
    return case(
        (age_days < 1, _AGING_BUCKET_LABELS[0]),
        (age_days < 3, _AGING_BUCKET_LABELS[1]),
        (age_days < 7, _AGING_BUCKET_LABELS[2]),
        (age_days < 14, _AGING_BUCKET_LABELS[3]),
        (age_days < 30, _AGING_BUCKET_LABELS[4]),
        else_=_AGING_BUCKET_LABELS[5],
    )


def get_issue_aging_buckets(
    db: Session, *, jurisdiction_code: str | None = None
) -> dict[str, int] | None:
    """Age-since-created_at distribution for currently ACTIVE issues only
    (excludes CLOSED/REJECTED -- a closed issue is no longer aging
    backlog). One GROUP BY query; every bucket label is present,
    zero-filled if empty, in a fixed deterministic order.
    jurisdiction_code scopes to that jurisdiction's subtree via
    Issue.jurisdiction_id; returns None if the code doesn't match any
    real jurisdiction (caller maps that to 404).
    """
    bucket = _aging_bucket_expr()
    counts: dict[str, int] = {label: 0 for label in _AGING_BUCKET_LABELS}
    query = db.query(bucket, func.count(Issue.id)).filter(
        Issue.status.notin_(TERMINAL_STATUSES)
    )
    if jurisdiction_code is not None:
        subtree_ids = get_jurisdiction_subtree_ids(db, jurisdiction_code)
        if subtree_ids is None:
            return None
        query = query.filter(Issue.jurisdiction_id.in_(subtree_ids))
    rows = query.group_by(bucket).all()
    for label, count in rows:
        counts[label] = count
    return counts


def get_resolution_timing_metrics(
    db: Session, *, jurisdiction_code: str | None = None
) -> dict | None:
    """Resolution/closure timing metrics.

    Each average is computed ONLY over issues where the relevant
    timestamp(s) are actually present -- e.g. avg_time_to_resolve_hours
    averages over issues with a non-null resolved_at, and is None (never
    0) if no issue has ever been resolved. SQL's AVG() already ignores
    NULL inputs, so an issue missing resolved_at simply doesn't
    contribute to that average rather than skewing it toward zero. All
    three metrics are single SQL AVG(julianday(...) - julianday(...))
    aggregates -- no rows are pulled into Python for this calculation.
    jurisdiction_code scopes both queries to that jurisdiction's subtree
    via Issue.jurisdiction_id; returns None if the code doesn't match any
    real jurisdiction (caller maps that to 404).
    """
    subtree_ids = None
    if jurisdiction_code is not None:
        subtree_ids = get_jurisdiction_subtree_ids(db, jurisdiction_code)
        if subtree_ids is None:
            return None

    resolved_query = db.query(
        func.count(Issue.id),
        func.avg(func.julianday(Issue.resolved_at) - func.julianday(Issue.created_at)) * 24,
    ).filter(Issue.resolved_at.isnot(None))
    closed_query = db.query(
        func.count(Issue.id),
        func.avg(func.julianday(Issue.closed_at) - func.julianday(Issue.resolved_at)) * 24,
        func.avg(func.julianday(Issue.closed_at) - func.julianday(Issue.created_at)) * 24,
    ).filter(Issue.closed_at.isnot(None))
    if subtree_ids is not None:
        resolved_query = resolved_query.filter(Issue.jurisdiction_id.in_(subtree_ids))
        closed_query = closed_query.filter(Issue.jurisdiction_id.in_(subtree_ids))

    resolved_count, avg_resolve_hours = resolved_query.one()
    closed_count, avg_close_after_resolution_hours, avg_close_from_creation_hours = (
        closed_query.one()
    )
    return {
        "resolved_issue_count": resolved_count,
        "avg_time_to_resolve_hours": avg_resolve_hours,
        "closed_issue_count": closed_count,
        "avg_time_to_close_after_resolution_hours": avg_close_after_resolution_hours,
        "avg_time_to_close_from_creation_hours": avg_close_from_creation_hours,
    }


def get_department_workload(
    db: Session, *, jurisdiction_code: str | None = None
) -> list[tuple[Department, dict[IssueStatus, int]]] | None:
    """Per-department status breakdown for EVERY active department, in a
    single query -- avoids the N+1 that would result from calling
    get_department_issue_counts() once per department to build the same
    picture. Every department gets every IssueStatus zero-filled, even
    departments with zero currently-assigned issues. Returned in
    Department.code order.

    jurisdiction_code scopes to departments whose OWN jurisdiction
    (Department.jurisdiction_id, set at department creation -- Milestone
    13) falls within that jurisdiction's subtree -- department workload
    is inherently a per-department view, so scoping is by which
    departments belong to the jurisdiction, not by each issue's own
    jurisdiction_id. Returns None if the code doesn't match any real
    jurisdiction (caller maps that to 404).
    """
    query = (
        db.query(Department, Issue.status, func.count(Issue.id))
        .outerjoin(Issue, Issue.assigned_department_id == Department.id)
        .filter(Department.is_active.is_(True))
    )
    if jurisdiction_code is not None:
        subtree_ids = get_jurisdiction_subtree_ids(db, jurisdiction_code)
        if subtree_ids is None:
            return None
        query = query.filter(Department.jurisdiction_id.in_(subtree_ids))
    rows = query.group_by(Department.id, Issue.status).order_by(Department.code).all()

    by_department: dict[int, dict[IssueStatus, int]] = {}
    departments_by_id: dict[int, Department] = {}
    order: list[int] = []

    for department, status_value, count in rows:
        if department.id not in by_department:
            by_department[department.id] = {s: 0 for s in IssueStatus}
            departments_by_id[department.id] = department
            order.append(department.id)
        if status_value is not None:
            # None only occurs for a department with zero issues, via the
            # outer join -- nothing to record for that placeholder row.
            by_department[department.id][status_value] = count

    return [(departments_by_id[dept_id], by_department[dept_id]) for dept_id in order]


def get_recent_activity(
    db: Session, limit: int = 20, *, jurisdiction_code: str | None = None
) -> list[tuple[IssueStatusHistory, Issue]] | None:
    """Most recent status-transition events, newest first.

    A single JOIN query -- Issue.public_id/category come from the join
    itself, not a per-row lookup, so this stays one query regardless of
    `limit`. jurisdiction_code scopes to that jurisdiction's subtree via
    Issue.jurisdiction_id; returns None if the code doesn't match any
    real jurisdiction (caller maps that to 404).
    """
    query = db.query(IssueStatusHistory, Issue).join(Issue, IssueStatusHistory.issue_id == Issue.id)
    if jurisdiction_code is not None:
        subtree_ids = get_jurisdiction_subtree_ids(db, jurisdiction_code)
        if subtree_ids is None:
            return None
        query = query.filter(Issue.jurisdiction_id.in_(subtree_ids))
    return (
        query.order_by(IssueStatusHistory.changed_at.desc(), IssueStatusHistory.id.desc())
        .limit(limit)
        .all()
    )


# --- Civic Accountability & Intelligence (Milestone 10) ----------------------
#
# Two small, single-purpose aggregate queries that Milestone 9's existing
# functions don't already provide. Everything else insight generation
# needs (department workload, stale counts, status funnel) reuses M8/M9
# functions directly -- see backend/service.py's get_civic_insights().


def count_active_high_severity_issues(
    db: Session, *, jurisdiction_code: str | None = None
) -> dict[SeverityLevel, int] | None:
    """Count of currently-active (per METRIC_INACTIVE_STATUSES -- excludes
    RESOLVED/CLOSED/REJECTED) issues at CRITICAL/HIGH severity, by
    severity. One GROUP BY query; both severities are present, zero-filled
    if unused. jurisdiction_code scopes to that jurisdiction's subtree via
    Issue.jurisdiction_id; returns None if the code doesn't match any real
    jurisdiction (caller maps that to 404).
    """
    high_severities = (SeverityLevel.CRITICAL, SeverityLevel.HIGH)
    counts: dict[SeverityLevel, int] = {s: 0 for s in high_severities}
    query = db.query(Issue.severity, func.count(Issue.id)).filter(
        Issue.severity.in_(high_severities), Issue.status.notin_(METRIC_INACTIVE_STATUSES)
    )
    if jurisdiction_code is not None:
        subtree_ids = get_jurisdiction_subtree_ids(db, jurisdiction_code)
        if subtree_ids is None:
            return None
        query = query.filter(Issue.jurisdiction_id.in_(subtree_ids))
    rows = query.group_by(Issue.severity).all()
    for severity_value, count in rows:
        counts[severity_value] = count
    return counts


def count_active_issues_by_category(
    db: Session, *, jurisdiction_code: str | None = None
) -> dict[IssueCategory, int] | None:
    """Count of currently-active (per METRIC_INACTIVE_STATUSES -- excludes
    RESOLVED/CLOSED/REJECTED) issues, by AI category. One GROUP BY query;
    every IssueCategory is present, zero-filled if unused. jurisdiction_code
    scopes to that jurisdiction's subtree via Issue.jurisdiction_id; returns
    None if the code doesn't match any real jurisdiction (caller maps that
    to 404).
    """
    counts: dict[IssueCategory, int] = {c: 0 for c in IssueCategory}
    query = db.query(Issue.category, func.count(Issue.id)).filter(
        Issue.status.notin_(METRIC_INACTIVE_STATUSES)
    )
    if jurisdiction_code is not None:
        subtree_ids = get_jurisdiction_subtree_ids(db, jurisdiction_code)
        if subtree_ids is None:
            return None
        query = query.filter(Issue.jurisdiction_id.in_(subtree_ids))
    rows = query.group_by(Issue.category).all()
    for category_value, count in rows:
        counts[category_value] = count
    return counts


# --- Jurisdiction hierarchy (Milestone 13) -----------------------------------
#
# Read-only for now (the registry is seeded via Alembic, same pattern as
# Department). Kept deliberately small: list + ancestry are the only two
# operations the API/frontend need.


def get_jurisdiction_by_code(db: Session, code: str) -> Jurisdiction | None:
    """Fetch a single Jurisdiction by its public code, or None."""
    return db.query(Jurisdiction).filter(Jurisdiction.code == code).one_or_none()


def list_jurisdictions(
    db: Session,
    *,
    level: JurisdictionLevel | None = None,
    country_code: str | None = None,
) -> list[Jurisdiction]:
    """Active jurisdictions, optionally filtered by level and/or country.

    Eager-loads `parent` (a to-one relationship, so this can't multiply
    rows) in the same query -- avoids an N+1 if a caller needs each row's
    parent_code, matching the pattern already used for
    Issue.assigned_department elsewhere in this module.
    """
    query = db.query(Jurisdiction).options(joinedload(Jurisdiction.parent)).filter(
        Jurisdiction.is_active.is_(True)
    )
    if level is not None:
        query = query.filter(Jurisdiction.level == level)
    if country_code is not None:
        query = query.filter(Jurisdiction.country_code == country_code)
    return query.order_by(Jurisdiction.level, Jurisdiction.code).all()


def _walk_ancestry(
    jurisdiction: Jurisdiction, all_by_id: dict[int, Jurisdiction]
) -> list[Jurisdiction]:
    """Pure, DB-free ancestry walk given an already-loaded id->Jurisdiction
    map. Root-to-leaf order."""
    chain: list[Jurisdiction] = []
    current: Jurisdiction | None = all_by_id.get(jurisdiction.id)
    while current is not None:
        chain.append(current)
        current = (
            all_by_id.get(current.parent_jurisdiction_id)
            if current.parent_jurisdiction_id is not None
            else None
        )
    return list(reversed(chain))


def get_jurisdiction_ancestry(db: Session, jurisdiction: Jurisdiction) -> list[Jurisdiction]:
    """Root-to-leaf ancestry chain for a single jurisdiction, e.g.
    [India, West Bengal, Nadia, Krishnanagar Municipality].

    Loads the entire jurisdictions table ONCE (it's small by design -- a
    handful of administrative levels, not a GIS dataset) and walks the
    parent chain in Python. One query regardless of hierarchy depth.

    For resolving ancestry for MULTIPLE jurisdictions at once (e.g. a
    list endpoint), use get_jurisdictions_with_ancestry below instead --
    calling this function once per row would re-load the whole table on
    every call, which is exactly the N+1 shape this module otherwise
    avoids.
    """
    all_by_id = {j.id: j for j in db.query(Jurisdiction).all()}
    return _walk_ancestry(jurisdiction, all_by_id)


def get_jurisdictions_with_ancestry(
    db: Session,
    *,
    level: JurisdictionLevel | None = None,
    country_code: str | None = None,
) -> list[tuple[Jurisdiction, list[Jurisdiction]]]:
    """Active jurisdictions (optionally filtered), each paired with its
    ancestry chain. Exactly two queries total -- one for the filtered
    list, one for the whole table (reused to resolve every row's
    ancestry) -- regardless of how many jurisdictions are returned. This
    is what backend.service.get_jurisdictions uses instead of calling
    get_jurisdiction_ancestry once per row.
    """
    jurisdictions = list_jurisdictions(db, level=level, country_code=country_code)
    all_by_id = {j.id: j for j in db.query(Jurisdiction).all()}
    return [(j, _walk_ancestry(j, all_by_id)) for j in jurisdictions]
