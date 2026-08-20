"""
Centralized status-transition rules for Issue lifecycle management.

This is the single source of truth for which Issue.status transitions are
permitted. Anything that moves an Issue between states (currently just
backend/repository.py's transition_issue_status) must go through
validate_transition() here rather than encoding rules inline in a route
or service function.
"""

from __future__ import annotations

from backend.models import IssueStatus

# Maps each status to the set of statuses it may transition into.
# Any (from_status, to_status) pair not present here is disallowed --
# including every transition out of CLOSED and REJECTED, which are
# terminal states.
VALID_TRANSITIONS: dict[IssueStatus, frozenset[IssueStatus]] = {
    IssueStatus.SUBMITTED: frozenset({IssueStatus.CLASSIFIED, IssueStatus.REJECTED}),
    IssueStatus.CLASSIFIED: frozenset({IssueStatus.ROUTED, IssueStatus.REJECTED}),
    IssueStatus.ROUTED: frozenset({IssueStatus.ACKNOWLEDGED}),
    IssueStatus.ACKNOWLEDGED: frozenset({IssueStatus.IN_PROGRESS}),
    IssueStatus.IN_PROGRESS: frozenset({IssueStatus.RESOLVED}),
    IssueStatus.RESOLVED: frozenset({IssueStatus.CLOSED, IssueStatus.REOPENED}),
    IssueStatus.REOPENED: frozenset({IssueStatus.IN_PROGRESS}),
    IssueStatus.CLOSED: frozenset(),
    IssueStatus.REJECTED: frozenset(),
}


class InvalidTransitionError(Exception):
    """Raised when a requested status transition is not permitted."""

    def __init__(self, from_status: IssueStatus, to_status: IssueStatus) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"Cannot transition issue from {from_status.value} to {to_status.value}."
        )


def validate_transition(from_status: IssueStatus, to_status: IssueStatus) -> None:
    """Raise InvalidTransitionError unless from_status -> to_status is allowed."""
    if to_status not in VALID_TRANSITIONS.get(from_status, frozenset()):
        raise InvalidTransitionError(from_status, to_status)
