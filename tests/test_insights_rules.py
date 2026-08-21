"""
Unit tests for backend/insights.py's pure, DB-free deterministic insight
rules. No database, no Gemini -- just the threshold logic in isolation.
"""

from ai.schemas import SeverityLevel
from backend.insights import (
    build_department_concentration_insights,
    build_high_severity_insight,
    build_lifecycle_bottleneck_insight,
    build_recurring_category_insight,
    build_stale_concentration_insight,
)
from backend.models import IssueStatus

TERMINAL = frozenset({IssueStatus.CLOSED, IssueStatus.REJECTED})


def _zero_status_counts() -> dict[IssueStatus, int]:
    return {s: 0 for s in IssueStatus}


# --- High severity ------------------------------------------------------------


def test_high_severity_no_qualifying_issues_returns_none():
    assert build_high_severity_insight({SeverityLevel.CRITICAL: 0, SeverityLevel.HIGH: 0}) is None


def test_high_severity_with_critical_is_high_priority():
    insight = build_high_severity_insight({SeverityLevel.CRITICAL: 1, SeverityLevel.HIGH: 0})
    assert insight.priority.value == "high"
    assert insight.affected_issue_count == 1
    assert insight.evidence == {"critical_count": 1, "high_count": 0}


def test_high_severity_with_only_high_is_medium_priority():
    insight = build_high_severity_insight({SeverityLevel.CRITICAL: 0, SeverityLevel.HIGH: 2})
    assert insight.priority.value == "medium"
    assert insight.affected_issue_count == 2


def test_high_severity_evidence_matches_counts_exactly():
    insight = build_high_severity_insight({SeverityLevel.CRITICAL: 3, SeverityLevel.HIGH: 5})
    assert insight.evidence["critical_count"] == 3
    assert insight.evidence["high_count"] == 5
    assert insight.affected_issue_count == 8


# --- Department workload concentration ----------------------------------------


def test_department_concentration_empty_list():
    assert build_department_concentration_insights([]) == []


def test_department_concentration_all_zero_no_insights():
    result = build_department_concentration_insights([("A", "Dept A", 0), ("B", "Dept B", 0)])
    assert result == []


def test_department_concentration_flags_outlier():
    result = build_department_concentration_insights(
        [("A", "Dept A", 10), ("B", "Dept B", 1), ("C", "Dept C", 1)]
    )
    assert len(result) == 1
    assert result[0].affected_department.code == "A"
    assert result[0].affected_issue_count == 10


def test_department_concentration_below_min_count_not_flagged():
    # Even if proportionally an outlier, below DEPARTMENT_CONCENTRATION_MIN_COUNT.
    result = build_department_concentration_insights([("A", "Dept A", 2), ("B", "Dept B", 0)])
    assert result == []


def test_department_concentration_roughly_even_workload_not_flagged():
    result = build_department_concentration_insights(
        [("A", "Dept A", 4), ("B", "Dept B", 4), ("C", "Dept C", 3)]
    )
    assert result == []


def test_department_concentration_evidence_includes_average():
    result = build_department_concentration_insights(
        [("A", "Dept A", 10), ("B", "Dept B", 1), ("C", "Dept C", 1)]
    )
    assert result[0].evidence["department_average"] == round((10 + 1 + 1) / 3, 2)


# --- Stale concentration -------------------------------------------------------


def test_stale_below_minimum_returns_none():
    assert build_stale_concentration_insight(2, older_than_hours=48) is None


def test_stale_at_minimum_returns_insight():
    insight = build_stale_concentration_insight(3, older_than_hours=48)
    assert insight is not None
    assert insight.affected_issue_count == 3
    assert insight.evidence == {"stale_count": 3, "older_than_hours": 48}


def test_stale_zero_returns_none():
    assert build_stale_concentration_insight(0, older_than_hours=48) is None


# --- Recurring category --------------------------------------------------------


def test_recurring_category_insufficient_total_returns_none():
    # Total of 4 active issues is below RECURRING_CATEGORY_MIN_ACTIVE_TOTAL (5).
    assert build_recurring_category_insight({"Street Lighting": 3, "Other": 1}) is None


def test_recurring_category_enough_data_but_no_dominant_category_returns_none():
    # 5 total, evenly split -- no category reaches the 25% share... actually
    # each holds 20%, below RECURRING_CATEGORY_MIN_SHARE.
    counts = {"A": 1, "B": 1, "C": 1, "D": 1, "E": 1}
    assert build_recurring_category_insight(counts) is None


def test_recurring_category_dominant_category_flagged():
    insight = build_recurring_category_insight({"Street Lighting": 4, "Other": 1})
    assert insight is not None
    assert insight.evidence["category"] == "Street Lighting"
    assert insight.affected_issue_count == 4


def test_recurring_category_all_zero_returns_none():
    assert build_recurring_category_insight({"A": 0, "B": 0}) is None


# --- Lifecycle bottleneck -------------------------------------------------------


def test_bottleneck_insufficient_active_total_returns_none():
    counts = _zero_status_counts()
    counts[IssueStatus.SUBMITTED] = 2  # below BOTTLENECK_MIN_ACTIVE_TOTAL (3)
    assert build_lifecycle_bottleneck_insight(counts, TERMINAL) is None


def test_bottleneck_no_dominant_status_returns_none():
    counts = _zero_status_counts()
    counts[IssueStatus.SUBMITTED] = 2
    counts[IssueStatus.CLASSIFIED] = 2
    counts[IssueStatus.ROUTED] = 2
    # Each holds 1/3 (~33%), below BOTTLENECK_MIN_SHARE (40%).
    assert build_lifecycle_bottleneck_insight(counts, TERMINAL) is None


def test_bottleneck_dominant_status_flagged():
    counts = _zero_status_counts()
    counts[IssueStatus.IN_PROGRESS] = 5
    counts[IssueStatus.SUBMITTED] = 1
    insight = build_lifecycle_bottleneck_insight(counts, TERMINAL)
    assert insight is not None
    assert insight.evidence["status"] == "IN_PROGRESS"
    assert insight.affected_issue_count == 5


def test_bottleneck_excludes_terminal_statuses_from_calculation():
    counts = _zero_status_counts()
    counts[IssueStatus.CLOSED] = 100  # must not count toward "active" total
    counts[IssueStatus.SUBMITTED] = 1
    counts[IssueStatus.CLASSIFIED] = 1
    # Only 2 active issues total -- below BOTTLENECK_MIN_ACTIVE_TOTAL (3),
    # even though CLOSED has a huge count.
    assert build_lifecycle_bottleneck_insight(counts, TERMINAL) is None
