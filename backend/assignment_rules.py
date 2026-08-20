"""
Centralized rules for whether an Issue can currently receive an official
department assignment, and department-assignment domain exceptions.

Kept separate from backend/transitions.py: assignment eligibility is a
related but distinct concept from status-transition legality. Assigning a
department never invents a new lifecycle transition -- when a transition
is warranted (CLASSIFIED -> ROUTED), it goes through the existing
centralized transition system in backend/transitions.py like any other
transition. This module only decides *whether assignment itself is
allowed* for the issue's current status.
"""

from __future__ import annotations

from backend.models import IssueStatus

# Statuses where assigning/reassigning a department is allowed WITHOUT
# altering the Issue's lifecycle status at all (per the spec: "if the
# issue is already ROUTED or later in the lifecycle, do not automatically
# reset/restart its status"). REOPENED is included here too -- it's an
# active operational state (an issue that was resolved but reopened still
# needs department engagement), not a terminal one.
ASSIGNABLE_WITHOUT_TRANSITION: frozenset[IssueStatus] = frozenset(
    {
        IssueStatus.ROUTED,
        IssueStatus.ACKNOWLEDGED,
        IssueStatus.IN_PROGRESS,
        IssueStatus.RESOLVED,
        IssueStatus.CLOSED,
        IssueStatus.REOPENED,
    }
)


class AssignmentNotAllowedError(Exception):
    """Raised when an Issue's current status doesn't permit assignment."""

    def __init__(self, status: IssueStatus, message: str) -> None:
        self.status = status
        super().__init__(message)


class DepartmentNotFoundError(Exception):
    """Raised when a department_code doesn't match any known department."""

    def __init__(self, department_code: str) -> None:
        self.department_code = department_code
        super().__init__(f"No department found with code {department_code!r}.")


class DepartmentInactiveError(Exception):
    """Raised when a department_code matches a department that is inactive."""

    def __init__(self, department_code: str) -> None:
        self.department_code = department_code
        super().__init__(
            f"Department {department_code!r} is not active and cannot be assigned."
        )


def check_assignment_allowed(status: IssueStatus) -> None:
    """Raise AssignmentNotAllowedError if `status` doesn't permit assignment.

    CLASSIFIED is allowed but not listed in ASSIGNABLE_WITHOUT_TRANSITION --
    it's a special case that also triggers the CLASSIFIED -> ROUTED
    transition; the caller (backend.repository.assign_department_to_issue)
    handles that separately via the existing centralized transition system.

    SUBMITTED and REJECTED are the two statuses where assignment is
    refused entirely:
    - SUBMITTED: the spec is explicit that assignment must not silently
      invent a SUBMITTED -> ROUTED transition; the issue must be
      classified first.
    - REJECTED: a terminal, non-actionable outcome -- routing a rejected
      report to a department doesn't make operational sense.
    """
    if status == IssueStatus.CLASSIFIED or status in ASSIGNABLE_WITHOUT_TRANSITION:
        return

    if status == IssueStatus.SUBMITTED:
        raise AssignmentNotAllowedError(
            status,
            "Issue must be classified before it can be routed to a department. "
            "Transition it to CLASSIFIED first.",
        )

    # REJECTED is the only remaining status.
    raise AssignmentNotAllowedError(
        status,
        f"A {status.value.lower()} issue cannot be assigned to a department.",
    )
