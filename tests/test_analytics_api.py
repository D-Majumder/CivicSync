"""
API-level tests for CivicSync's Civic Intelligence & Analytics API
(Milestone 9): GET /api/admin/analytics/funnel, .../severity-distribution,
.../department-workload, .../aging, .../resolution-timing,
.../recent-activity.

analyze_complaint() is mocked (no Gemini calls anywhere in this file).
Every test uses the `client` fixture, which wires the FastAPI app to an
isolated, temporary SQLite database via a get_db dependency override --
never civicsync.db.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from ai.schemas import CivicIssue, IssueCategory, SeverityLevel
from backend.database import Base, build_engine, get_db
from backend.main import app
from backend.models import Department


def _sample_civic_issue(**overrides) -> CivicIssue:
    base = dict(
        original_text="There has been no street light near our school for two weeks.",
        category=IssueCategory.STREET_LIGHTING,
        problem="Lack of functioning street light near a school.",
        location="near our school",
        severity=SeverityLevel.MEDIUM,
        confidence=0.82,
        suggested_department="Electrical / Street Lighting Department",
    )
    base.update(overrides)
    return CivicIssue(**base)


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "test_analytics_api.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    seed_db = TestingSessionLocal()
    seed_db.add_all(
        [
            Department(code="STREET_LIGHTING", name="Street Lighting", is_active=True),
            Department(code="ELECTRICITY", name="Electricity", is_active=True),
            Department(code="ROADS_TRANSPORT", name="Roads & Transport", is_active=True),
        ]
    )
    seed_db.commit()
    seed_db.close()

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    test_client = TestClient(app)
    test_client.engine = engine
    test_client.SessionLocal = TestingSessionLocal
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _submit_issue(client, **overrides) -> dict:
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue(**overrides)):
        response = client.post("/api/issues", json={"text": "Some complaint."})
    assert response.status_code == 201
    return response.json()


def _advance(client, public_id: str, *targets: str) -> dict:
    body = None
    for target in targets:
        response = client.post(f"/api/issues/{public_id}/status", json={"status": target})
        assert response.status_code == 200, response.json()
        body = response.json()
    return body


def _assign(client, public_id: str, department_code: str) -> dict:
    response = client.post(
        f"/api/issues/{public_id}/assignment", json={"department_code": department_code}
    )
    assert response.status_code == 200, response.json()
    return response.json()


def _set_column(client, public_id: str, column: str, value: datetime | None) -> None:
    """Directly UPDATE a timestamp column via raw SQL, bypassing ORM
    hooks, to construct specific timing/aging scenarios for tests."""
    db = client.SessionLocal()
    try:
        db.execute(
            text(f"UPDATE issues SET {column} = :v WHERE public_id = :pid"),
            {"v": value.isoformat(sep=" ") if value is not None else None, "pid": public_id},
        )
        db.commit()
    finally:
        db.close()


def _backdate_created_at(client, public_id: str, hours_ago: float) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).replace(tzinfo=None)
    _set_column(client, public_id, "created_at", cutoff)


def _count_queries(client, path: str) -> tuple[int, dict]:
    executed = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        executed.append(statement)

    event.listen(client.engine, "before_cursor_execute", _record)
    try:
        response = client.get(path)
    finally:
        event.remove(client.engine, "before_cursor_execute", _record)

    assert response.status_code == 200
    return len(executed), response.json()


# ============================================================================
# STATUS FUNNEL -- GET /api/admin/analytics/funnel
# ============================================================================


def test_funnel_empty_database_returns_zero_counts(client):
    body = client.get("/api/admin/analytics/funnel").json()
    assert body["total_issues"] == 0
    assert all(stage["count"] == 0 for stage in body["stages"])


def test_funnel_reflects_current_status_counts(client):
    a = _submit_issue(client)
    _submit_issue(client)
    _advance(client, a["public_id"], "CLASSIFIED")

    body = client.get("/api/admin/analytics/funnel").json()
    counts = {stage["status"]: stage["count"] for stage in body["stages"]}
    assert counts["SUBMITTED"] == 1
    assert counts["CLASSIFIED"] == 1
    assert body["total_issues"] == 2


def test_funnel_stage_order_is_deterministic(client):
    body = client.get("/api/admin/analytics/funnel").json()
    statuses = [stage["status"] for stage in body["stages"]]
    assert statuses == [
        "SUBMITTED", "CLASSIFIED", "ROUTED", "ACKNOWLEDGED",
        "IN_PROGRESS", "RESOLVED", "CLOSED", "REJECTED", "REOPENED",
    ]


def test_funnel_includes_friendly_status_label(client):
    body = client.get("/api/admin/analytics/funnel").json()
    labels = {stage["status"]: stage["status_label"] for stage in body["stages"]}
    assert labels["IN_PROGRESS"] == "In Progress"


def test_funnel_does_not_call_gemini(client):
    _submit_issue(client)
    with (
        patch("backend.main.analyze_complaint") as mock_main,
        patch("backend.service.analyze_complaint") as mock_service,
    ):
        response = client.get("/api/admin/analytics/funnel")
    assert response.status_code == 200
    mock_main.assert_not_called()
    mock_service.assert_not_called()


# ============================================================================
# SEVERITY DISTRIBUTION -- GET /api/admin/analytics/severity-distribution
# ============================================================================


def test_severity_distribution_empty_database(client):
    body = client.get("/api/admin/analytics/severity-distribution").json()
    assert body["total_issues"] == 0
    assert all(
        entry["count"] == 0 and entry["percentage"] == 0.0 for entry in body["distribution"]
    )


def test_severity_distribution_percentages_correct(client):
    _submit_issue(client, severity=SeverityLevel.HIGH)
    _submit_issue(client, severity=SeverityLevel.HIGH)
    _submit_issue(client, severity=SeverityLevel.LOW)
    _submit_issue(client, severity=SeverityLevel.LOW)

    body = client.get("/api/admin/analytics/severity-distribution").json()
    by_severity = {e["severity"]: e for e in body["distribution"]}
    assert by_severity["High"]["count"] == 2
    assert by_severity["High"]["percentage"] == pytest.approx(50.0)
    assert by_severity["Low"]["count"] == 2
    assert by_severity["Low"]["percentage"] == pytest.approx(50.0)
    assert by_severity["Critical"]["percentage"] == 0.0


def test_severity_distribution_uses_existing_severity_level_values_only(client):
    body = client.get("/api/admin/analytics/severity-distribution").json()
    severities = {e["severity"] for e in body["distribution"]}
    assert severities == {"Low", "Medium", "High", "Critical", "Unknown"}


def test_severity_distribution_does_not_call_gemini(client):
    _submit_issue(client)
    with (
        patch("backend.main.analyze_complaint") as mock_main,
        patch("backend.service.analyze_complaint") as mock_service,
    ):
        response = client.get("/api/admin/analytics/severity-distribution")
    assert response.status_code == 200
    mock_main.assert_not_called()
    mock_service.assert_not_called()


# ============================================================================
# DEPARTMENT WORKLOAD -- GET /api/admin/analytics/department-workload
# ============================================================================


def test_department_workload_empty_database_includes_all_active_departments(client):
    body = client.get("/api/admin/analytics/department-workload").json()
    codes = {d["code"] for d in body["departments"]}
    assert codes == {"STREET_LIGHTING", "ELECTRICITY", "ROADS_TRANSPORT"}
    assert all(d["total_assigned"] == 0 for d in body["departments"])


def test_department_workload_correct_counts_across_multiple_departments(client):
    a = _submit_issue(client)
    b = _submit_issue(client)
    c = _submit_issue(client)
    for issue in (a, b, c):
        _advance(client, issue["public_id"], "CLASSIFIED")
    _assign(client, a["public_id"], "STREET_LIGHTING")
    _assign(client, b["public_id"], "STREET_LIGHTING")
    _assign(client, c["public_id"], "ELECTRICITY")

    body = client.get("/api/admin/analytics/department-workload").json()
    by_code = {d["code"]: d for d in body["departments"]}
    assert by_code["STREET_LIGHTING"]["total_assigned"] == 2
    assert by_code["ELECTRICITY"]["total_assigned"] == 1
    assert by_code["ROADS_TRANSPORT"]["total_assigned"] == 0


def test_department_workload_active_resolved_closed_rollup(client):
    active = _submit_issue(client)
    _advance(client, active["public_id"], "CLASSIFIED")
    _assign(client, active["public_id"], "STREET_LIGHTING")

    closed = _submit_issue(client)
    _advance(client, closed["public_id"], "CLASSIFIED")
    _assign(client, closed["public_id"], "STREET_LIGHTING")
    _advance(client, closed["public_id"], "ACKNOWLEDGED", "IN_PROGRESS", "RESOLVED", "CLOSED")

    body = client.get("/api/admin/analytics/department-workload").json()
    entry = next(d for d in body["departments"] if d["code"] == "STREET_LIGHTING")
    assert entry["active_issues"] == 1
    assert entry["closed_issues"] == 1
    assert entry["by_status"]["ROUTED"] == 1
    assert entry["by_status"]["CLOSED"] == 1


def test_department_workload_deterministic_code_order(client):
    body = client.get("/api/admin/analytics/department-workload").json()
    codes = [d["code"] for d in body["departments"]]
    assert codes == sorted(codes)


def test_department_workload_no_n_plus_1_query(client):
    """One query for all departments' workload, regardless of how many
    active departments exist or how many issues each has."""
    for _ in range(3):
        issue = _submit_issue(client)
        _advance(client, issue["public_id"], "CLASSIFIED")
        _assign(client, issue["public_id"], "STREET_LIGHTING")

    query_count, body = _count_queries(client, "/api/admin/analytics/department-workload")
    assert len(body["departments"]) == 3
    # A single grouped query (plus small constant overhead), not one
    # query per department.
    assert query_count <= 5


def test_department_workload_does_not_call_gemini(client):
    with (
        patch("backend.main.analyze_complaint") as mock_main,
        patch("backend.service.analyze_complaint") as mock_service,
    ):
        response = client.get("/api/admin/analytics/department-workload")
    assert response.status_code == 200
    mock_main.assert_not_called()
    mock_service.assert_not_called()


# ============================================================================
# AGING BUCKETS -- GET /api/admin/analytics/aging
# ============================================================================


def test_aging_empty_database_zero_filled(client):
    body = client.get("/api/admin/analytics/aging").json()
    assert body["total_active_issues"] == 0
    assert [b["bucket"] for b in body["buckets"]] == [
        "0-1 day", "1-3 days", "3-7 days", "7-14 days", "14-30 days", "30+ days",
    ]
    assert all(b["count"] == 0 for b in body["buckets"])


def test_aging_excludes_closed_and_rejected_issues(client):
    closed = _submit_issue(client)
    _advance(
        client, closed["public_id"],
        "CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS", "RESOLVED", "CLOSED",
    )
    _backdate_created_at(client, closed["public_id"], hours_ago=100)

    rejected = _submit_issue(client)
    _advance(client, rejected["public_id"], "REJECTED")
    _backdate_created_at(client, rejected["public_id"], hours_ago=100)

    body = client.get("/api/admin/analytics/aging").json()
    assert body["total_active_issues"] == 0


def test_aging_boundary_just_under_one_day(client):
    issue = _submit_issue(client)
    _backdate_created_at(client, issue["public_id"], hours_ago=23)

    body = client.get("/api/admin/analytics/aging").json()
    by_bucket = {b["bucket"]: b["count"] for b in body["buckets"]}
    assert by_bucket["0-1 day"] == 1
    assert by_bucket["1-3 days"] == 0


def test_aging_boundary_just_over_one_day(client):
    issue = _submit_issue(client)
    _backdate_created_at(client, issue["public_id"], hours_ago=25)

    body = client.get("/api/admin/analytics/aging").json()
    by_bucket = {b["bucket"]: b["count"] for b in body["buckets"]}
    assert by_bucket["1-3 days"] == 1
    assert by_bucket["0-1 day"] == 0


def test_aging_30_plus_days_bucket(client):
    issue = _submit_issue(client)
    _backdate_created_at(client, issue["public_id"], hours_ago=24 * 45)

    body = client.get("/api/admin/analytics/aging").json()
    by_bucket = {b["bucket"]: b["count"] for b in body["buckets"]}
    assert by_bucket["30+ days"] == 1


def test_aging_does_not_call_gemini(client):
    with (
        patch("backend.main.analyze_complaint") as mock_main,
        patch("backend.service.analyze_complaint") as mock_service,
    ):
        response = client.get("/api/admin/analytics/aging")
    assert response.status_code == 200
    mock_main.assert_not_called()
    mock_service.assert_not_called()


# ============================================================================
# RESOLUTION TIMING -- GET /api/admin/analytics/resolution-timing
# ============================================================================


def test_resolution_timing_no_resolved_issues_returns_null_averages(client):
    _submit_issue(client)  # still SUBMITTED, no resolved_at/closed_at
    body = client.get("/api/admin/analytics/resolution-timing").json()
    assert body["resolved_issue_count"] == 0
    assert body["avg_time_to_resolve_hours"] is None
    assert body["closed_issue_count"] == 0
    assert body["avg_time_to_close_after_resolution_hours"] is None
    assert body["avg_time_to_close_from_creation_hours"] is None


def test_resolution_timing_avg_resolve_hours_correct(client):
    issue = _submit_issue(client)
    _advance(
        client, issue["public_id"],
        "CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS", "RESOLVED",
    )
    # created_at 10 hours before "now"; resolved_at defaults to "now" from
    # the transition -- expect avg_time_to_resolve_hours ~= 10.
    _backdate_created_at(client, issue["public_id"], hours_ago=10)

    body = client.get("/api/admin/analytics/resolution-timing").json()
    assert body["resolved_issue_count"] == 1
    assert body["avg_time_to_resolve_hours"] == pytest.approx(10, abs=0.05)


def test_resolution_timing_avg_close_after_resolution_correct(client):
    issue = _submit_issue(client)
    _advance(
        client, issue["public_id"],
        "CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS", "RESOLVED",
    )
    resolved_at = datetime.now(timezone.utc) - timedelta(hours=5)
    _set_column(client, issue["public_id"], "resolved_at", resolved_at.replace(tzinfo=None))
    _advance(client, issue["public_id"], "CLOSED")

    body = client.get("/api/admin/analytics/resolution-timing").json()
    assert body["closed_issue_count"] == 1
    assert body["avg_time_to_close_after_resolution_hours"] == pytest.approx(5, abs=0.05)


def test_resolution_timing_unresolved_issues_dont_skew_average(client):
    resolved = _submit_issue(client)
    _advance(
        client, resolved["public_id"],
        "CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS", "RESOLVED",
    )
    _backdate_created_at(client, resolved["public_id"], hours_ago=8)

    _submit_issue(client)  # never resolved -- must not pull the average toward 0
    _submit_issue(client)

    body = client.get("/api/admin/analytics/resolution-timing").json()
    assert body["resolved_issue_count"] == 1
    assert body["avg_time_to_resolve_hours"] == pytest.approx(8, abs=0.05)


def test_resolution_timing_does_not_call_gemini(client):
    with (
        patch("backend.main.analyze_complaint") as mock_main,
        patch("backend.service.analyze_complaint") as mock_service,
    ):
        response = client.get("/api/admin/analytics/resolution-timing")
    assert response.status_code == 200
    mock_main.assert_not_called()
    mock_service.assert_not_called()


# ============================================================================
# RECENT ACTIVITY -- GET /api/admin/analytics/recent-activity
# ============================================================================


def test_recent_activity_empty_database(client):
    body = client.get("/api/admin/analytics/recent-activity").json()
    assert body["activities"] == []


def test_recent_activity_returns_newest_first(client):
    issue = _submit_issue(client)
    _advance(client, issue["public_id"], "CLASSIFIED", "ROUTED")

    body = client.get("/api/admin/analytics/recent-activity").json()
    to_statuses = [a["to_status"] for a in body["activities"]]
    assert to_statuses[0] == "ROUTED"
    assert to_statuses[-1] == "SUBMITTED"


def test_recent_activity_limit_works(client):
    issue = _submit_issue(client)
    _advance(client, issue["public_id"], "CLASSIFIED", "ROUTED", "ACKNOWLEDGED")

    body = client.get("/api/admin/analytics/recent-activity", params={"limit": 2}).json()
    assert len(body["activities"]) == 2


def test_recent_activity_includes_public_id_and_category(client):
    issue = _submit_issue(client, category=IssueCategory.STREET_LIGHTING)
    body = client.get("/api/admin/analytics/recent-activity").json()
    entry = body["activities"][0]
    assert entry["public_id"] == issue["public_id"]
    assert entry["category"] == "Street Lighting"


def test_recent_activity_excludes_internal_ids(client):
    _submit_issue(client)
    body = client.get("/api/admin/analytics/recent-activity").json()
    for entry in body["activities"]:
        assert "id" not in entry
        assert "issue_id" not in entry


def test_recent_activity_does_not_call_gemini(client):
    with (
        patch("backend.main.analyze_complaint") as mock_main,
        patch("backend.service.analyze_complaint") as mock_service,
    ):
        response = client.get("/api/admin/analytics/recent-activity")
    assert response.status_code == 200
    mock_main.assert_not_called()
    mock_service.assert_not_called()


# ============================================================================
# PRIVACY -- analytics never leak through the public tracking endpoint
# ============================================================================


def test_public_tracking_response_has_no_analytics_fields(client):
    issue = _submit_issue(client)
    body = client.get(f"/api/track/{issue['public_id']}").json()
    for forbidden in (
        "by_status", "by_severity", "by_department", "stages", "buckets",
        "distribution", "workload", "activities", "avg_time_to_resolve_hours",
    ):
        assert forbidden not in body
