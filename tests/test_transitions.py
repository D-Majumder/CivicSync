"""Unit tests for backend/transitions.py's centralized transition rules."""

import pytest

from backend.models import IssueStatus
from backend.transitions import VALID_TRANSITIONS, InvalidTransitionError, validate_transition

PRIMARY_LIFECYCLE = [
    (IssueStatus.SUBMITTED, IssueStatus.CLASSIFIED),
    (IssueStatus.CLASSIFIED, IssueStatus.ROUTED),
    (IssueStatus.ROUTED, IssueStatus.ACKNOWLEDGED),
    (IssueStatus.ACKNOWLEDGED, IssueStatus.IN_PROGRESS),
    (IssueStatus.IN_PROGRESS, IssueStatus.RESOLVED),
    (IssueStatus.RESOLVED, IssueStatus.CLOSED),
]

SPECIAL_TRANSITIONS = [
    (IssueStatus.SUBMITTED, IssueStatus.REJECTED),
    (IssueStatus.CLASSIFIED, IssueStatus.REJECTED),
    (IssueStatus.RESOLVED, IssueStatus.REOPENED),
    (IssueStatus.REOPENED, IssueStatus.IN_PROGRESS),
]

INVALID_TRANSITIONS = [
    (IssueStatus.CLOSED, IssueStatus.IN_PROGRESS),
    (IssueStatus.CLOSED, IssueStatus.RESOLVED),
    (IssueStatus.SUBMITTED, IssueStatus.RESOLVED),
    (IssueStatus.SUBMITTED, IssueStatus.CLOSED),
    (IssueStatus.ROUTED, IssueStatus.RESOLVED),
    (IssueStatus.ACKNOWLEDGED, IssueStatus.CLOSED),
    (IssueStatus.REJECTED, IssueStatus.SUBMITTED),
    (IssueStatus.REJECTED, IssueStatus.CLASSIFIED),
    (IssueStatus.CLOSED, IssueStatus.CLASSIFIED),
    (IssueStatus.IN_PROGRESS, IssueStatus.SUBMITTED),
]


@pytest.mark.parametrize("from_status,to_status", PRIMARY_LIFECYCLE + SPECIAL_TRANSITIONS)
def test_valid_transitions_are_accepted(from_status, to_status):
    validate_transition(from_status, to_status)  # must not raise


@pytest.mark.parametrize("from_status,to_status", INVALID_TRANSITIONS)
def test_invalid_transitions_are_rejected(from_status, to_status):
    with pytest.raises(InvalidTransitionError):
        validate_transition(from_status, to_status)


def test_closed_cannot_transition_anywhere():
    for candidate in IssueStatus:
        with pytest.raises(InvalidTransitionError):
            validate_transition(IssueStatus.CLOSED, candidate)


def test_rejected_cannot_transition_anywhere():
    for candidate in IssueStatus:
        with pytest.raises(InvalidTransitionError):
            validate_transition(IssueStatus.REJECTED, candidate)


def test_every_status_has_an_explicit_entry_in_the_transition_table():
    """Guards against a future new IssueStatus silently falling back to the
    empty-set default and becoming permanently un-transitionable."""
    assert set(VALID_TRANSITIONS.keys()) == set(IssueStatus)


def test_invalid_transition_error_message_names_both_statuses():
    with pytest.raises(InvalidTransitionError) as exc_info:
        validate_transition(IssueStatus.CLOSED, IssueStatus.RESOLVED)
    assert "CLOSED" in str(exc_info.value)
    assert "RESOLVED" in str(exc_info.value)
