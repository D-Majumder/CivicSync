"""
API-level tests for CivicSync's Civic Accountability & Intelligence API
(Milestone 10): GET /api/admin/insights, POST /api/admin/insights/prioritize.

Gemini is mocked in every test in this file -- no real Gemini calls. The
one real-Gemini integration test for this milestone (if any) lives
separately and is gated exactly like the existing ai/client.py integration
test (RUN_GEMINI_INTEGRATION_TESTS=1 + a real key), never run by default.

Every test uses the `client` fixture, which wires the FastAPI app to an
isolated, temporary SQLite database via a get_db dependency override --
never civicsync.db.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from ai.schemas import CivicIssue, InsightPrioritizationOutput, IssueCategory, SeverityLevel
from backend.database import Base, build_engine, get_db
from backend.auth import get_current_authority
from backend.main import app
from backend.models import Department
from google.genai.errors import APIError


def _sample_civic_issue(**overrides) -> CivicIssue:
    base = dict(
        original_text="There has been no street light near our school for two weeks.",
        category=IssueCategory.STREET_LIGHTING,
        problem="Lack of functioning street light near a school.",
        severity=SeverityLevel.MEDIUM,
        confidence=0.82,
        suggested_department="Electrical / Street Lighting Department",
    )
    base.update(overrides)
    return CivicIssue(**base)


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "test_insights_api.db"
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
    app.dependency_overrides[get_current_authority] = lambda: "test-authority"
    test_client = TestClient(app)
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


# ============================================================================
# GET /api/admin/insights -- deterministic, no Gemini
# ============================================================================


def test_empty_database_returns_no_insights(client):
    body = client.get("/api/admin/insights").json()
    assert body["insights"] == []
    assert "generated_at" in body


def test_no_qualifying_insights_with_light_activity(client):
    """A single, unremarkable low-severity issue shouldn't trigger any
    insight -- nothing here should be fabricated from routine data."""
    _submit_issue(client, severity=SeverityLevel.LOW)
    body = client.get("/api/admin/insights").json()
    assert body["insights"] == []


def test_high_severity_unresolved_insight_appears(client):
    _submit_issue(client, severity=SeverityLevel.CRITICAL)
    body = client.get("/api/admin/insights").json()
    types = {i["insight_type"] for i in body["insights"]}
    assert "high_severity_unresolved" in types


def test_high_severity_insight_evidence_matches_actual_counts(client):
    _submit_issue(client, severity=SeverityLevel.CRITICAL)
    _submit_issue(client, severity=SeverityLevel.CRITICAL)
    _submit_issue(client, severity=SeverityLevel.HIGH)
    body = client.get("/api/admin/insights").json()
    insight = next(i for i in body["insights"] if i["insight_type"] == "high_severity_unresolved")
    assert insight["evidence"]["critical_count"] == 2
    assert insight["evidence"]["high_count"] == 1
    assert insight["affected_issue_count"] == 3


def test_department_workload_concentration_insight(client):
    # One department gets 4 active issues, others get none -- a clear outlier.
    for _ in range(4):
        issue = _submit_issue(client)
        _advance(client, issue["public_id"], "CLASSIFIED")
        _assign(client, issue["public_id"], "STREET_LIGHTING")

    body = client.get("/api/admin/insights").json()
    dept_insight = next(
        (i for i in body["insights"] if i["insight_type"] == "department_workload_concentration"),
        None,
    )
    assert dept_insight is not None
    assert dept_insight["affected_department"]["code"] == "STREET_LIGHTING"
    assert "id" not in dept_insight["affected_department"]


def test_stale_issue_concentration_insight(client):
    public_ids = []
    for _ in range(3):
        issue = _submit_issue(client)
        public_ids.append(issue["public_id"])

    db = client.SessionLocal()
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import text

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat(sep=" ")
    for public_id in public_ids:
        db.execute(
            text("UPDATE issues SET updated_at = :ts WHERE public_id = :pid"),
            {"ts": cutoff, "pid": public_id},
        )
    db.commit()
    db.close()

    body = client.get("/api/admin/insights").json()
    stale_insight = next(
        (i for i in body["insights"] if i["insight_type"] == "stale_issue_concentration"), None
    )
    assert stale_insight is not None
    assert stale_insight["affected_issue_count"] == 3


def test_lifecycle_bottleneck_insight(client):
    for _ in range(5):
        issue = _submit_issue(client)
        _advance(
            client, issue["public_id"],
            "CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS",
        )
    _submit_issue(client)  # stays SUBMITTED, keeps IN_PROGRESS the dominant stage

    body = client.get("/api/admin/insights").json()
    bottleneck = next(
        (i for i in body["insights"] if i["insight_type"] == "lifecycle_bottleneck"), None
    )
    assert bottleneck is not None
    assert bottleneck["evidence"]["status"] == "IN_PROGRESS"


def test_recurring_category_with_enough_data(client):
    for _ in range(4):
        _submit_issue(client, category=IssueCategory.STREET_LIGHTING)
    _submit_issue(client, category=IssueCategory.OTHER)

    body = client.get("/api/admin/insights").json()
    recurring = next(
        (i for i in body["insights"] if i["insight_type"] == "recurring_category_concentration"),
        None,
    )
    assert recurring is not None
    assert recurring["evidence"]["category"] == "Street Lighting"


def test_recurring_category_insufficient_historical_data(client):
    """Fewer than the minimum active-issue sample size must never produce
    a recurring-category insight, even if one category happens to be all
    that exists so far."""
    _submit_issue(client, category=IssueCategory.STREET_LIGHTING)
    _submit_issue(client, category=IssueCategory.STREET_LIGHTING)

    body = client.get("/api/admin/insights").json()
    types = {i["insight_type"] for i in body["insights"]}
    assert "recurring_category_concentration" not in types


def test_insights_statistics_are_deterministic_across_repeated_calls(client):
    _submit_issue(client, severity=SeverityLevel.CRITICAL)
    first = client.get("/api/admin/insights").json()
    second = client.get("/api/admin/insights").json()
    first_insights = [{k: v for k, v in i.items()} for i in first["insights"]]
    second_insights = [{k: v for k, v in i.items()} for i in second["insights"]]
    assert first_insights == second_insights


def test_insights_no_fabricated_counts_for_absent_signals(client):
    """A department with zero issues must never appear in a concentration
    insight, and stale/bottleneck insights must not appear with no data
    to support them."""
    _submit_issue(client, severity=SeverityLevel.LOW)  # routine, low signal
    body = client.get("/api/admin/insights").json()
    for insight in body["insights"]:
        assert insight["affected_issue_count"] > 0


def test_insights_excludes_internal_ids(client):
    _submit_issue(client, severity=SeverityLevel.CRITICAL)
    body = client.get("/api/admin/insights").json()
    raw_text = client.get("/api/admin/insights").text
    assert '"id"' not in raw_text
    for insight in body["insights"]:
        if insight["affected_department"]:
            assert "id" not in insight["affected_department"]


def test_get_insights_does_not_call_gemini(client):
    _submit_issue(client, severity=SeverityLevel.CRITICAL)
    with (
        patch("backend.main.analyze_complaint") as mock_main,
        patch("backend.service.analyze_complaint") as mock_service,
        patch("backend.service.generate_insight_prioritization") as mock_prioritize,
    ):
        response = client.get("/api/admin/insights")
    assert response.status_code == 200
    mock_main.assert_not_called()
    mock_service.assert_not_called()
    mock_prioritize.assert_not_called()


# ============================================================================
# POST /api/admin/insights/prioritize -- Gemini-assisted, mocked
# ============================================================================


def test_prioritize_with_no_insights_never_calls_gemini(client):
    with patch("backend.service.generate_insight_prioritization") as mock_prioritize:
        response = client.post("/api/admin/insights/prioritize")
    assert response.status_code == 200
    body = response.json()
    assert body["insights"] == []
    assert body["ai_recommendation"] is None
    mock_prioritize.assert_not_called()


def test_prioritize_gemini_success_path(client):
    _submit_issue(client, severity=SeverityLevel.CRITICAL)
    fake_output = InsightPrioritizationOutput(
        recommended_priority_order=["high_severity_unresolved"],
        summary="Address the critical issue first.",
        explanation="It is the only critical, unresolved issue on record.",
    )
    with patch("backend.service.generate_insight_prioritization", return_value=fake_output):
        response = client.post("/api/admin/insights/prioritize")
    assert response.status_code == 200
    body = response.json()
    assert body["ai_recommendation"]["summary"] == "Address the critical issue first."
    assert body["ai_recommendation"]["recommended_priority_order"] == ["high_severity_unresolved"]
    assert body["ai_recommendation"]["ai_generated"] is True
    assert body["ai_recommendation_error"] is None
    # Grounded insights are returned unchanged alongside the recommendation.
    assert len(body["insights"]) >= 1


def test_prioritize_gemini_environment_error_degrades_gracefully(client):
    _submit_issue(client, severity=SeverityLevel.CRITICAL)
    with patch(
        "backend.service.generate_insight_prioritization",
        side_effect=EnvironmentError("GEMINI_API_KEY is not set. secret-looking-value"),
    ):
        response = client.post("/api/admin/insights/prioritize")
    assert response.status_code == 200
    body = response.json()
    assert body["ai_recommendation"] is None
    assert body["ai_recommendation_error"] == "AI prioritization is temporarily unavailable."
    assert "secret-looking-value" not in response.text
    assert "GEMINI_API_KEY" not in response.text
    # Grounded insights still present -- their value doesn't depend on Gemini.
    assert len(body["insights"]) >= 1


def test_prioritize_gemini_api_error_degrades_gracefully(client):
    _submit_issue(client, severity=SeverityLevel.CRITICAL)
    api_error = APIError(503, {"message": "internal detail that should not leak"})
    with patch("backend.service.generate_insight_prioritization", side_effect=api_error):
        response = client.post("/api/admin/insights/prioritize")
    assert response.status_code == 200
    body = response.json()
    assert body["ai_recommendation"] is None
    assert body["ai_recommendation_error"] == "AI prioritization is temporarily unavailable."
    assert "internal detail" not in response.text


def test_prioritize_malformed_gemini_output_is_sanitized(client):
    """Gemini hallucinating an insight_type that was never sent, or
    omitting one that was, must never leak through or drop real
    insights."""
    _submit_issue(client, severity=SeverityLevel.CRITICAL)
    for _ in range(4):
        issue = _submit_issue(client, category=IssueCategory.STREET_LIGHTING)
    _submit_issue(client, category=IssueCategory.OTHER)

    fake_output = InsightPrioritizationOutput(
        recommended_priority_order=[
            "totally_hallucinated_insight_type",
            "high_severity_unresolved",
            # recurring_category_concentration intentionally omitted --
            # must still be appended by the sanitizer.
        ],
        summary="Focus on severity first.",
        explanation="Critical severity issues take precedence.",
    )
    with patch("backend.service.generate_insight_prioritization", return_value=fake_output):
        response = client.post("/api/admin/insights/prioritize")

    body = response.json()
    order = body["ai_recommendation"]["recommended_priority_order"]
    assert "totally_hallucinated_insight_type" not in order
    real_types = {i["insight_type"] for i in body["insights"]}
    assert set(order) == real_types  # every real insight present, nothing invented


def test_ai_recommendation_never_mutates_official_issue_state(client):
    """The prioritization call is read-only end to end -- an issue's
    status/assignment must be byte-for-byte identical before and after."""
    issue = _submit_issue(client, severity=SeverityLevel.CRITICAL)
    before = client.get(f"/api/issues/{issue['public_id']}").json()

    fake_output = InsightPrioritizationOutput(
        recommended_priority_order=["high_severity_unresolved"],
        summary="s",
        explanation="e",
    )
    with patch("backend.service.generate_insight_prioritization", return_value=fake_output):
        client.post("/api/admin/insights/prioritize")

    after = client.get(f"/api/issues/{issue['public_id']}").json()
    assert before == after


def test_prioritize_does_not_call_analyze_complaint(client):
    """The prioritization Gemini call is a distinct capability from
    complaint analysis -- it must never trigger analyze_complaint."""
    _submit_issue(client, severity=SeverityLevel.CRITICAL)
    fake_output = InsightPrioritizationOutput(
        recommended_priority_order=["high_severity_unresolved"], summary="s", explanation="e"
    )
    with (
        patch("backend.main.analyze_complaint") as mock_main,
        patch("backend.service.analyze_complaint") as mock_service,
        patch("backend.service.generate_insight_prioritization", return_value=fake_output),
    ):
        response = client.post("/api/admin/insights/prioritize")
    assert response.status_code == 200
    mock_main.assert_not_called()
    mock_service.assert_not_called()


def test_prioritize_excludes_internal_ids(client):
    _submit_issue(client, severity=SeverityLevel.CRITICAL)
    fake_output = InsightPrioritizationOutput(
        recommended_priority_order=["high_severity_unresolved"], summary="s", explanation="e"
    )
    with patch("backend.service.generate_insight_prioritization", return_value=fake_output):
        response = client.post("/api/admin/insights/prioritize")
    assert '"id"' not in response.text


# ============================================================================
# Privacy: public tracking endpoint is unaffected by Milestone 10
# ============================================================================


def test_public_tracking_endpoint_unchanged_by_insights(client):
    issue = _submit_issue(client, severity=SeverityLevel.CRITICAL)
    body = client.get(f"/api/track/{issue['public_id']}").json()
    for forbidden in (
        "insight_type", "insights", "ai_recommendation", "evidence",
        "recommended_action", "priority",
    ):
        assert forbidden not in body


def test_health_still_works(client):
    assert client.get("/health").status_code == 200


def test_insights_no_n_plus_1_query(client, tmp_path):
    """GET /api/admin/insights should execute a small, roughly constant
    number of SQL statements regardless of how many issues exist."""
    from sqlalchemy import event

    for i in range(15):
        issue = _submit_issue(client, severity=SeverityLevel.HIGH if i % 3 == 0 else SeverityLevel.LOW)
        if i % 2 == 0:
            _advance(client, issue["public_id"], "CLASSIFIED")
            _assign(client, issue["public_id"], "STREET_LIGHTING")

    executed = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        executed.append(statement)

    # Reach into the same engine the fixture built.
    engine = client.SessionLocal.kw["bind"]
    event.listen(engine, "before_cursor_execute", _record)
    try:
        response = client.get("/api/admin/insights")
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert response.status_code == 200
    # 5 insight generators, each backed by at most a couple of aggregate
    # queries -- nowhere near one query per issue for 15 issues.
    assert len(executed) <= 15
