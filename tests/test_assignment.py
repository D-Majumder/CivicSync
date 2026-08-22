"""
Repository/service-level tests for official department assignment. Uses
the `db_session` fixture (temporary SQLite database, never civicsync.db).
No Gemini calls anywhere in this file.
"""

from unittest.mock import patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from ai.schemas import CivicIssue, IssueCategory, SeverityLevel
from backend.assignment_rules import AssignmentNotAllowedError, DepartmentInactiveError
from backend.models import Department, Issue, IssueStatus, Jurisdiction, JurisdictionLevel
from backend.repository import (
    assign_department_to_issue,
    create_issue_from_civic_issue,
    get_assignment_history,
    get_current_assignment,
    get_issue_by_public_id,
    transition_issue_status,
)
from backend.service import assign_issue_department


def _sample_civic_issue(**overrides) -> CivicIssue:
    base = dict(
        original_text="There has been no street light near our school for two weeks.",
        category=IssueCategory.STREET_LIGHTING,
        problem="Non-functional street light near a school.",
        severity=SeverityLevel.MEDIUM,
        confidence=0.8,
        suggested_department="Electrical / Street Lighting Department",
    )
    base.update(overrides)
    return CivicIssue(**base)


def _get_or_create_jurisdiction_id(db_session) -> int:
    """Milestone 17: Issue.jurisdiction_id is required. Reuses an
    existing seeded jurisdiction if this db_session already has one
    (idempotent across multiple _create_issue calls in the same test)."""
    existing = (
        db_session.query(Jurisdiction)
        .filter(Jurisdiction.code == "IN-WB-NADIA-KRISHNANAGAR")
        .one_or_none()
    )
    if existing is not None:
        return existing.id
    jurisdiction = Jurisdiction(
        code="IN-WB-NADIA-KRISHNANAGAR", name="Krishnanagar Municipality",
        level=JurisdictionLevel.LOCAL_BODY, country_code="IN",
    )
    db_session.add(jurisdiction)
    db_session.commit()
    return jurisdiction.id


def _create_issue(db_session, **overrides) -> Issue:
    jurisdiction_id = _get_or_create_jurisdiction_id(db_session)
    return create_issue_from_civic_issue(db_session, _sample_civic_issue(**overrides), jurisdiction_id)


def _create_department(db_session, code="STREET_LIGHTING", name="Street Lighting", **kw) -> Department:
    department = Department(code=code, name=name, **kw)
    db_session.add(department)
    db_session.commit()
    db_session.refresh(department)
    return department


def _advance(db_session, issue: Issue, *targets: IssueStatus) -> Issue:
    for target in targets:
        issue = transition_issue_status(db_session, issue, target)
    return issue


# --- 5, 6, 7: basic assignment succeeds and is returned correctly -----------


def test_assignment_with_valid_department_succeeds(db_session):
    issue = _create_issue(db_session)
    _advance(db_session, issue, IssueStatus.CLASSIFIED)
    department = _create_department(db_session)

    updated = assign_department_to_issue(db_session, issue, department, reason="Routed.")
    assert updated.assigned_department is not None
    assert updated.assigned_department.code == "STREET_LIGHTING"


def test_assigned_department_returned_correctly(db_session):
    issue = _create_issue(db_session)
    _advance(db_session, issue, IssueStatus.CLASSIFIED)
    department = _create_department(db_session, code="ELECTRICITY", name="Electricity")

    updated = assign_department_to_issue(db_session, issue, department)
    assert updated.assigned_department.code == "ELECTRICITY"
    assert updated.assigned_department.name == "Electricity"


def test_suggested_department_unchanged_after_assignment(db_session):
    issue = _create_issue(
        db_session, suggested_department="Electrical / Street Lighting Department"
    )
    _advance(db_session, issue, IssueStatus.CLASSIFIED)
    department = _create_department(db_session, code="ELECTRICITY", name="Electricity")

    updated = assign_department_to_issue(db_session, issue, department)
    assert updated.suggested_department == "Electrical / Street Lighting Department"
    # Different department than the AI's own suggestion -- proves this is
    # a real, independent, authorized decision, not a copy of the AI text.
    assert updated.assigned_department.code == "ELECTRICITY"


# --- 8: assignment does not call Gemini --------------------------------------


def test_assign_department_service_does_not_call_gemini(db_session):
    issue = _create_issue(db_session)
    _advance(db_session, issue, IssueStatus.CLASSIFIED)
    _create_department(db_session)

    with patch("backend.service.analyze_complaint") as mock_analyze:
        assign_issue_department(db_session, issue.public_id, "STREET_LIGHTING")

    mock_analyze.assert_not_called()


# --- 9: CLASSIFIED + assignment transitions to ROUTED ------------------------


def test_classified_issue_transitions_to_routed_on_assignment(db_session):
    issue = _create_issue(db_session)
    _advance(db_session, issue, IssueStatus.CLASSIFIED)
    department = _create_department(db_session)

    updated = assign_department_to_issue(db_session, issue, department)
    assert updated.status == IssueStatus.ROUTED


# --- 10: ROUTED + reassignment does not reset lifecycle ----------------------


def test_routed_issue_reassignment_does_not_reset_status(db_session):
    issue = _create_issue(db_session)
    _advance(db_session, issue, IssueStatus.CLASSIFIED)
    street_lighting = _create_department(db_session)
    assign_department_to_issue(db_session, issue, street_lighting)
    assert issue.status == IssueStatus.ROUTED

    electricity = _create_department(db_session, code="ELECTRICITY", name="Electricity")
    updated = assign_department_to_issue(db_session, issue, electricity, reason="Reassigned.")
    assert updated.status == IssueStatus.ROUTED  # unchanged, not reset or bumped


@pytest.mark.parametrize(
    "target_status",
    [
        IssueStatus.ACKNOWLEDGED,
        IssueStatus.IN_PROGRESS,
        IssueStatus.RESOLVED,
        IssueStatus.CLOSED,
    ],
)
def test_later_lifecycle_issue_reassignment_does_not_reset_status(db_session, target_status):
    issue = _create_issue(db_session)
    path = [IssueStatus.CLASSIFIED, IssueStatus.ROUTED, IssueStatus.ACKNOWLEDGED]
    if target_status in (IssueStatus.IN_PROGRESS, IssueStatus.RESOLVED, IssueStatus.CLOSED):
        path.append(IssueStatus.IN_PROGRESS)
    if target_status in (IssueStatus.RESOLVED, IssueStatus.CLOSED):
        path.append(IssueStatus.RESOLVED)
    if target_status == IssueStatus.CLOSED:
        path.append(IssueStatus.CLOSED)
    _advance(db_session, issue, *path)
    assert issue.status == target_status

    department = _create_department(db_session)
    updated = assign_department_to_issue(db_session, issue, department)
    assert updated.status == target_status


# --- 11, 12, 13: reassignment history -----------------------------------------


def test_previous_assignment_receives_unassigned_at(db_session):
    issue = _create_issue(db_session)
    _advance(db_session, issue, IssueStatus.CLASSIFIED)
    street_lighting = _create_department(db_session)
    assign_department_to_issue(db_session, issue, street_lighting)

    first_assignment = get_current_assignment(db_session, issue)
    assert first_assignment.unassigned_at is None

    electricity = _create_department(db_session, code="ELECTRICITY", name="Electricity")
    assign_department_to_issue(db_session, issue, electricity)

    db_session.refresh(first_assignment)
    assert first_assignment.unassigned_at is not None


def test_new_assignment_receives_assigned_at(db_session):
    issue = _create_issue(db_session)
    _advance(db_session, issue, IssueStatus.CLASSIFIED)
    department = _create_department(db_session)

    assign_department_to_issue(db_session, issue, department)
    current = get_current_assignment(db_session, issue)
    assert current is not None
    assert current.assigned_at is not None
    assert current.unassigned_at is None


def test_assignment_history_contains_both_assignments(db_session):
    issue = _create_issue(db_session)
    _advance(db_session, issue, IssueStatus.CLASSIFIED)
    street_lighting = _create_department(db_session)
    electricity = _create_department(db_session, code="ELECTRICITY", name="Electricity")

    assign_department_to_issue(db_session, issue, street_lighting, reason="Initial routing.")
    assign_department_to_issue(db_session, issue, electricity, reason="Reassigned.")

    history = get_assignment_history(db_session, issue)
    assert len(history) == 2
    assert history[0].department.code == "STREET_LIGHTING"
    assert history[0].unassigned_at is not None
    assert history[1].department.code == "ELECTRICITY"
    assert history[1].unassigned_at is None


# --- 16, 17: invalid / inactive department ------------------------------------


def test_assignment_to_nonexistent_department_code_returns_none_lookup(db_session):
    """The repository layer works on Department objects directly; the
    department_code -> Department lookup and its 404 mapping live in the
    service layer (see backend.service.assign_issue_department)."""
    from backend.repository import get_department_by_code

    assert get_department_by_code(db_session, "NOT_A_REAL_DEPARTMENT") is None


def test_assignment_to_inactive_department_is_rejected(db_session):
    issue = _create_issue(db_session)
    _advance(db_session, issue, IssueStatus.CLASSIFIED)
    _create_department(db_session, is_active=False)

    with pytest.raises(DepartmentInactiveError):
        assign_issue_department(db_session, issue.public_id, "STREET_LIGHTING")


# --- 18: missing issue --------------------------------------------------------


def test_assign_issue_department_returns_none_for_missing_issue(db_session):
    _create_department(db_session)
    result = assign_issue_department(db_session, "CIV-DOES-NOT-EXIST", "STREET_LIGHTING")
    assert result is None


# --- 19: assignment reason is preserved ---------------------------------------


def test_assignment_reason_is_preserved(db_session):
    issue = _create_issue(db_session)
    _advance(db_session, issue, IssueStatus.CLASSIFIED)
    department = _create_department(db_session)

    assign_department_to_issue(
        db_session,
        issue,
        department,
        reason="Complaint routed to the department responsible for street lighting.",
    )
    current = get_current_assignment(db_session, issue)
    assert (
        current.reason
        == "Complaint routed to the department responsible for street lighting."
    )


# --- 20: reassignment does not alter AI suggested_department -----------------


def test_reassignment_does_not_alter_suggested_department(db_session):
    issue = _create_issue(
        db_session, suggested_department="Electrical / Street Lighting Department"
    )
    _advance(db_session, issue, IssueStatus.CLASSIFIED)
    street_lighting = _create_department(db_session)
    electricity = _create_department(db_session, code="ELECTRICITY", name="Electricity")

    assign_department_to_issue(db_session, issue, street_lighting)
    assign_department_to_issue(db_session, issue, electricity)

    assert issue.suggested_department == "Electrical / Street Lighting Department"


# --- 21, 22: atomicity ---------------------------------------------------------


def test_assignment_and_status_transition_are_atomic(db_session):
    """A single commit covers: closing any prior assignment, the new
    assignment history row, Issue.assigned_department_id, and (for a
    CLASSIFIED issue) the status change + its history row."""
    issue = _create_issue(db_session)
    _advance(db_session, issue, IssueStatus.CLASSIFIED)
    department = _create_department(db_session)

    updated = assign_department_to_issue(db_session, issue, department)

    assert updated.status == IssueStatus.ROUTED
    assert updated.assigned_department.code == "STREET_LIGHTING"
    history = get_assignment_history(db_session, updated)
    assert len(history) == 1


def test_failed_assignment_leaves_no_partial_history(db_session):
    """Simulate a persistence failure at commit time and confirm neither
    the assignment nor the status transition was left partially committed."""
    issue = _create_issue(db_session)
    _advance(db_session, issue, IssueStatus.CLASSIFIED)
    department = _create_department(db_session)

    with patch.object(
        db_session, "commit", side_effect=SQLAlchemyError("simulated commit failure")
    ):
        with pytest.raises(SQLAlchemyError):
            assign_issue_department(db_session, issue.public_id, "STREET_LIGHTING")

    db_session.expire_all()
    refreshed = get_issue_by_public_id(db_session, issue.public_id)
    assert refreshed.status == IssueStatus.CLASSIFIED
    assert refreshed.assigned_department is None
    assert get_assignment_history(db_session, refreshed) == []


def test_assignment_not_allowed_leaves_no_partial_state(db_session):
    """An AssignmentNotAllowedError (e.g. SUBMITTED) must not write
    anything -- not the assignment, not a history row."""
    issue = _create_issue(db_session)  # still SUBMITTED
    department = _create_department(db_session)

    with pytest.raises(AssignmentNotAllowedError):
        assign_department_to_issue(db_session, issue, department)

    assert issue.assigned_department is None
    assert get_assignment_history(db_session, issue) == []


# --- Assignment eligibility per status ----------------------------------------


def test_submitted_issue_cannot_be_assigned(db_session):
    issue = _create_issue(db_session)
    department = _create_department(db_session)
    with pytest.raises(AssignmentNotAllowedError):
        assign_department_to_issue(db_session, issue, department)


def test_rejected_issue_cannot_be_assigned(db_session):
    issue = _create_issue(db_session)
    _advance(db_session, issue, IssueStatus.REJECTED)
    department = _create_department(db_session)
    with pytest.raises(AssignmentNotAllowedError):
        assign_department_to_issue(db_session, issue, department)
