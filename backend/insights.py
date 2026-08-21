"""
Deterministic civic-insight generation rules for CivicSync.

Every function here is pure: it takes already-aggregated data (fetched by
backend/repository.py, orchestrated by backend/service.py) and decides
whether that data supports a grounded insight, and if so builds one. No
insight is ever fabricated from a threshold alone -- every threshold below
exists only to distinguish "a real, actionable pattern" from routine
day-to-day noise, and every insight's `evidence` traces directly back to
the counts that triggered it, so an authority (or a test) can verify the
reasoning without trusting a black box.

None of this touches Gemini -- these are plain counting/comparison rules
over data CivicSync already computes deterministically (see
backend/repository.py's Milestone 8/9 aggregate queries, reused here
rather than re-queried). Gemini is used only for turning these grounded
insights into a prioritized, explained recommendation -- never for the
counting itself (see backend/service.py's get_prioritized_insights and
ai/prompts.py's PRIORITIZATION_SYSTEM_PROMPT).

Note on scope: a "recent increase in complaint types over time" insight
was deliberately NOT implemented. Detecting a genuine trend needs two
comparable time windows with real temporal spread in created_at; most
CivicSync databases (and virtually every demo/test database) are seeded
in a single short burst, so a time-window comparison would either produce
meaningless noise or require inventing a plausible-looking trend from data
that doesn't actually support one -- exactly what this module exists to
avoid. Recurring-category CONCENTRATION (a snapshot, not a trend) is
implemented below instead, since it's reliably groundable from a single
point-in-time query.

Note on scope: a duration-based "issues stuck a long time in the same
status" bottleneck was also considered and not implemented separately --
Issue.updated_at already advances on every status transition, so this is
substantially the same signal GET /api/admin/issues/stale (Milestone 8)
already surfaces. build_lifecycle_bottleneck_insight below instead
measures status CONCENTRATION (many issues piled up in one stage,
regardless of how long they've been there), which is a genuinely distinct
signal rather than a duplicate of staleness.
"""

from __future__ import annotations

from ai.schemas import SeverityLevel
from backend.models import IssueStatus
from backend.schemas import DepartmentSummary, Insight, InsightPriority

# --- Thresholds --------------------------------------------------------------
# Plain, documented rules -- not government SLAs, not a statistical model.
# They exist only to separate "worth an authority's attention" from
# baseline activity that doesn't need a flag.

STALE_CONCENTRATION_MIN_COUNT = 3
"""Fewer than this many stale issues is routine, not a "concentration"."""

RECURRING_CATEGORY_MIN_ACTIVE_TOTAL = 5
"""Below this many active issues total, a category's share is too small a
sample to mean anything -- this is the "insufficient historical data"
guard."""

RECURRING_CATEGORY_MIN_SHARE = 0.25
"""A category holding at least this share of active issues is "recurring"."""

BOTTLENECK_MIN_ACTIVE_TOTAL = 3
"""Below this many active issues total, a status "concentration" isn't
meaningful -- same reasoning as RECURRING_CATEGORY_MIN_ACTIVE_TOTAL."""

BOTTLENECK_MIN_SHARE = 0.40
"""A single non-terminal status holding at least this share of active
issues is a bottleneck."""

DEPARTMENT_CONCENTRATION_MIN_COUNT = 3
"""Minimum active issues before a department's workload is even considered
for a concentration flag."""

DEPARTMENT_CONCENTRATION_MULTIPLIER = 1.5
"""A department is flagged when its active workload exceeds this multiple
of the average active workload across active departments."""


def build_high_severity_insight(high_severity_counts: dict[SeverityLevel, int]) -> Insight | None:
    """High-severity (Critical/High) issues that are currently active
    (not CLOSED/REJECTED). Returns None if there are none -- an insight is
    never manufactured from a zero count."""
    total = sum(high_severity_counts.values())
    if total == 0:
        return None

    critical = high_severity_counts.get(SeverityLevel.CRITICAL, 0)
    high = high_severity_counts.get(SeverityLevel.HIGH, 0)

    return Insight(
        insight_type="high_severity_unresolved",
        priority=InsightPriority.HIGH if critical > 0 else InsightPriority.MEDIUM,
        title="High-severity issues remain unresolved",
        summary=(
            f"{total} active issue(s) are Critical or High severity "
            f"({critical} Critical, {high} High)."
        ),
        affected_issue_count=total,
        affected_department=None,
        evidence={"critical_count": critical, "high_count": high},
        recommended_action="Review these issues for prioritized assignment or escalation.",
    )


def build_department_concentration_insights(
    department_active_counts: list[tuple[str, str, int]],
) -> list[Insight]:
    """One insight per department whose active workload is unusually large
    relative to the cross-department average.

    Args:
        department_active_counts: (code, name, active_issue_count) for
            every active department (including zero-workload ones).
    """
    counts = [count for _, _, count in department_active_counts]
    if not counts:
        return []

    average = sum(counts) / len(counts)
    if average <= 0:
        return []

    insights = []
    for code, name, count in department_active_counts:
        if count < DEPARTMENT_CONCENTRATION_MIN_COUNT:
            continue
        if count <= average * DEPARTMENT_CONCENTRATION_MULTIPLIER:
            continue
        insights.append(
            Insight(
                insight_type="department_workload_concentration",
                priority=InsightPriority.MEDIUM,
                title=f"{name} is carrying an unusually large active workload",
                summary=(
                    f"{name} has {count} active issue(s), compared to an average of "
                    f"{average:.1f} across active departments."
                ),
                affected_issue_count=count,
                affected_department=DepartmentSummary(code=code, name=name),
                evidence={"active_issues": count, "department_average": round(average, 2)},
                recommended_action=(
                    "Consider redistributing incoming issues or reviewing staffing for this department."
                ),
            )
        )
    return insights


def build_stale_concentration_insight(stale_count: int, older_than_hours: int) -> Insight | None:
    """A concentration of issues that haven't changed in `older_than_hours`.
    Returns None below STALE_CONCENTRATION_MIN_COUNT -- a couple of slow
    issues is routine, not a pattern worth flagging."""
    if stale_count < STALE_CONCENTRATION_MIN_COUNT:
        return None

    return Insight(
        insight_type="stale_issue_concentration",
        priority=InsightPriority.MEDIUM,
        title="A concentration of issues has not been updated recently",
        summary=(
            f"{stale_count} issue(s) have not changed status in at least "
            f"{older_than_hours} hours."
        ),
        affected_issue_count=stale_count,
        affected_department=None,
        evidence={"stale_count": stale_count, "older_than_hours": older_than_hours},
        recommended_action="Review these issues for follow-up or a status update.",
    )


def build_recurring_category_insight(category_active_counts: dict[str, int]) -> Insight | None:
    """The single most common category among active issues, if it holds a
    large enough share of a large enough sample to be meaningful. Returns
    None if there isn't enough active-issue volume overall (insufficient
    historical data) or no category dominates."""
    total = sum(category_active_counts.values())
    if total < RECURRING_CATEGORY_MIN_ACTIVE_TOTAL:
        return None

    top_category, top_count = max(category_active_counts.items(), key=lambda item: item[1])
    if top_count == 0:
        return None

    share = top_count / total
    if share < RECURRING_CATEGORY_MIN_SHARE:
        return None

    return Insight(
        insight_type="recurring_category_concentration",
        priority=InsightPriority.LOW,
        title=f'"{top_category}" is a recurring category among active issues',
        summary=(
            f'{top_count} of {total} active issue(s) ({share * 100:.0f}%) '
            f'are categorized as "{top_category}".'
        ),
        affected_issue_count=top_count,
        affected_department=None,
        evidence={
            "category": top_category,
            "count": top_count,
            "share_percent": round(share * 100, 1),
        },
        recommended_action=(
            "Consider whether a systemic or infrastructure-level fix would address this category directly."
        ),
    )


def build_lifecycle_bottleneck_insight(
    status_counts: dict[IssueStatus, int], terminal_statuses: frozenset[IssueStatus]
) -> Insight | None:
    """A single non-terminal status holding an unusually large share of
    all active issues -- a pipeline-stage bottleneck. Returns None if
    there isn't enough active-issue volume, or no single stage dominates.

    Args:
        status_counts: every IssueStatus mapped to its current count.
        terminal_statuses: the set of statuses to exclude as "active"
            (passed in rather than imported, to keep this module free of
            any backend.repository import).
    """
    active_counts = {
        status_value: count
        for status_value, count in status_counts.items()
        if status_value not in terminal_statuses
    }
    total_active = sum(active_counts.values())
    if total_active < BOTTLENECK_MIN_ACTIVE_TOTAL:
        return None

    bottleneck_status, bottleneck_count = max(active_counts.items(), key=lambda item: item[1])
    if bottleneck_count == 0:
        return None

    share = bottleneck_count / total_active
    if share < BOTTLENECK_MIN_SHARE:
        return None

    status_label = bottleneck_status.value.replace("_", " ").title()
    return Insight(
        insight_type="lifecycle_bottleneck",
        priority=InsightPriority.MEDIUM,
        title=f"Issues are concentrated in the {status_label} stage",
        summary=(
            f"{bottleneck_count} of {total_active} active issue(s) ({share * 100:.0f}%) "
            f"are currently {status_label}."
        ),
        affected_issue_count=bottleneck_count,
        affected_department=None,
        evidence={
            "status": bottleneck_status.value,
            "count": bottleneck_count,
            "share_percent": round(share * 100, 1),
        },
        recommended_action=f"Review why issues are accumulating at the {status_label} stage.",
    )
