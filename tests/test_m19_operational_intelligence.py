"""
Milestone 19: Operational Intelligence & Decision Support.

Reuses the established fixture patterns: a `client` fixture with
get_current_authority overridden, and a `real_auth_client` fixture with
the REAL dependency for authentication tests. Two independent
jurisdiction trees (Krishnanagar / Bengaluru) prove jurisdiction
isolation, matching the pattern from tests/test_operational_briefing_api.py
and tests/test_m18_phase5_reopening.py.

Gemini is mocked throughout -- no real Gemini calls.
"""

from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import sessionmaker

from ai.schemas import CivicIssue, IssueCategory, SeverityLevel
from backend.auth import get_current_authority, hash_password
from backend.database import Base, build_engine, get_db
from backend.main import app
from backend.models import Department, Issue, IssueStatus, Jurisdiction, JurisdictionLevel


def _sample_civic_issue(**overrides) -> CivicIssue:
    base = dict(
        original_text="Deep pothole on the main road, very dangerous.",
        category=IssueCategory.ROADS_AND_POTHOLES,
        problem="Large dangerous pothole.",
        severity=SeverityLevel.HIGH,
        confidence=0.9,
    )
    base.update(overrides)
    return CivicIssue(**base)


def _real_png_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (10, 10), color="red").save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CIVICSYNC_EVIDENCE_STORAGE_DIR", str(tmp_path / "evidence_storage"))
    db_path = tmp_path / "test_m19.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    seed_db = TestingSessionLocal()
    krishnanagar = Jurisdiction(
        code="IN-WB-NADIA-KRISHNANAGAR", name="Krishnanagar Municipality",
        level=JurisdictionLevel.LOCAL_BODY, country_code="IN",
    )
    seed_db.add(krishnanagar)
    seed_db.flush()
    bbmp = Jurisdiction(
        code="IN-KA-BLR-BBMP", name="BBMP",
        level=JurisdictionLevel.LOCAL_BODY, country_code="IN",
    )
    seed_db.add(bbmp)
    seed_db.flush()
    dept_a = Department(code="ROADS_TRANSPORT", name="Roads & Transport", jurisdiction_id=krishnanagar.id)
    dept_b = Department(code="STREET_LIGHTING", name="Street Lighting", jurisdiction_id=bbmp.id)
    seed_db.add_all([dept_a, dept_b])
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
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


@pytest.fixture
def real_auth_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTHORITY_USERNAME", "m19_authority")
    monkeypatch.setenv("AUTHORITY_PASSWORD_HASH", hash_password("m19-test-password"))
    monkeypatch.setenv("SESSION_SECRET", "m19-test-secret")
    db_path = tmp_path / "test_m19_auth.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    seed_db = TestingSessionLocal()
    seed_db.add(
        Jurisdiction(
            code="IN-WB-NADIA-KRISHNANAGAR", name="Krishnanagar Municipality",
            level=JurisdictionLevel.LOCAL_BODY, country_code="IN",
        )
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


def _advance_and_resolve(client, public_id, note="Fixed on-site."):
    for s in ["CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS"]:
        assert client.post(f"/api/issues/{public_id}/status", json={"status": s}).status_code == 200
    r = client.post(f"/api/issues/{public_id}/resolve", json={"resolution_note": note})
    assert r.status_code == 200
    return r.json()


def _get_intel(client, jurisdiction_code="IN-WB-NADIA-KRISHNANAGAR"):
    response = client.get(
        "/api/admin/analytics/resolution-intelligence",
        params={"jurisdiction_code": jurisdiction_code},
    )
    assert response.status_code == 200
    return response.json()


# ============================================================================
# Zero-data behavior
# ============================================================================


def test_zero_data_returns_none_rates_not_fabricated_zeros(client):
    body = _get_intel(client)
    assert body["total_resolved"] == 0
    assert body["resolution_rate_percent"] is None
    assert body["avg_resolution_hours"] is None
    assert body["median_resolution_hours"] is None
    assert body["reopen_rate_percent"] is None
    assert body["evidence_coverage_percent"] is None
    assert body["approved_reopens_by_category"] == []


# ============================================================================
# KPI calculations
# ============================================================================


def test_resolution_rate_calculation(client):
    a = _submit_issue(client)
    _submit_issue(client)  # left SUBMITTED, unresolved
    _advance_and_resolve(client, a["public_id"])

    body = _get_intel(client)
    assert body["total_resolved"] == 1
    # 1 resolved out of 2 non-rejected issues = 50%
    assert body["resolution_rate_percent"] == 50.0


def test_resolution_rate_excludes_rejected_issues(client):
    a = _submit_issue(client)
    b = _submit_issue(client)
    _advance_and_resolve(client, a["public_id"])
    client.post(f"/api/issues/{b['public_id']}/status", json={"status": "REJECTED"})

    body = _get_intel(client)
    # 1 resolved out of 1 non-rejected issue = 100%
    assert body["resolution_rate_percent"] == 100.0


def test_total_resolved_counts_issues_ever_resolved_including_reopened(client):
    """An issue that was resolved and then reopened still represents a
    completed resolution EVENT -- it must still count as "resolved" for
    this KPI, not disappear once its status changes to REOPENED."""
    a = _submit_issue(client)
    _advance_and_resolve(client, a["public_id"])
    req = client.post(
        f"/api/track/{a['public_id']}/reopen-request", json={"reason": "It broke again quickly."}
    ).json()
    client.post(
        f"/api/admin/reopen-requests/{req['public_id']}/decision", json={"approve": True}
    )

    body = _get_intel(client)
    assert body["total_resolved"] == 1
    assert body["currently_reopened"] == 1


# ============================================================================
# Resolution time
# ============================================================================


def test_average_resolution_time_computed(client):
    a = _submit_issue(client)
    _advance_and_resolve(client, a["public_id"])
    body = _get_intel(client)
    assert body["avg_resolution_hours"] is not None
    assert body["avg_resolution_hours"] >= 0


def test_median_resolution_time_with_multiple_resolved_issues(client):
    a = _submit_issue(client)
    b = _submit_issue(client)
    _advance_and_resolve(client, a["public_id"])
    _advance_and_resolve(client, b["public_id"])

    body = _get_intel(client)
    assert body["median_resolution_hours"] is not None
    assert body["avg_resolution_hours"] is not None


# ============================================================================
# Reopen counts / rate
# ============================================================================


def test_reopen_request_counts_by_state(client):
    a = _submit_issue(client)
    b = _submit_issue(client)
    c = _submit_issue(client)
    for issue in (a, b, c):
        _advance_and_resolve(client, issue["public_id"])

    req_a = client.post(f"/api/track/{a['public_id']}/reopen-request", json={"reason": "Broke again."}).json()
    req_b = client.post(f"/api/track/{b['public_id']}/reopen-request", json={"reason": "Not fixed."}).json()
    client.post(f"/api/track/{c['public_id']}/reopen-request", json={"reason": "Still an issue."})

    client.post(f"/api/admin/reopen-requests/{req_a['public_id']}/decision", json={"approve": True})
    client.post(f"/api/admin/reopen-requests/{req_b['public_id']}/decision", json={"approve": False})

    body = _get_intel(client)
    assert body["total_reopen_requests"] == 3
    assert body["pending_reopen_requests"] == 1
    assert body["approved_reopen_requests"] == 1
    assert body["rejected_reopen_requests"] == 1


def test_reopen_rate_among_resolved_issues(client):
    a = _submit_issue(client)
    b = _submit_issue(client)
    _advance_and_resolve(client, a["public_id"])
    _advance_and_resolve(client, b["public_id"])

    req = client.post(f"/api/track/{a['public_id']}/reopen-request", json={"reason": "Broke again."}).json()
    client.post(f"/api/admin/reopen-requests/{req['public_id']}/decision", json={"approve": True})

    body = _get_intel(client)
    # 1 approved reopen out of 2 resolved issues = 50%
    assert body["reopen_rate_percent"] == 50.0


def test_approved_reopens_by_category_breakdown(client):
    a = _submit_issue(client, category=IssueCategory.ROADS_AND_POTHOLES)
    _advance_and_resolve(client, a["public_id"])
    req = client.post(f"/api/track/{a['public_id']}/reopen-request", json={"reason": "Broke again."}).json()
    client.post(f"/api/admin/reopen-requests/{req['public_id']}/decision", json={"approve": True})

    body = _get_intel(client)
    categories = {row["category"]: row["count"] for row in body["approved_reopens_by_category"]}
    assert categories.get("Roads and Potholes") == 1


# ============================================================================
# Evidence coverage
# ============================================================================


def test_evidence_coverage_counts_issues_not_files(client):
    a = _submit_issue(client)
    b = _submit_issue(client)
    _advance_and_resolve(client, a["public_id"])
    _advance_and_resolve(client, b["public_id"])

    # Upload TWO evidence files to the same issue -- must still count as
    # ONE resolution with evidence, not two.
    client.post(
        f"/api/issues/{a['public_id']}/evidence",
        files={"file": ("p1.png", _real_png_bytes(), "image/png")},
    )
    client.post(
        f"/api/issues/{a['public_id']}/evidence",
        files={"file": ("p2.png", _real_png_bytes(), "image/png")},
    )

    body = _get_intel(client)
    assert body["resolutions_with_evidence"] == 1
    assert body["resolutions_without_evidence"] == 1
    assert body["evidence_coverage_percent"] == 50.0


# ============================================================================
# Jurisdiction isolation
# ============================================================================


def test_jurisdiction_isolation_between_two_trees(client):
    a = _submit_issue(client)  # default jurisdiction: Krishnanagar
    _advance_and_resolve(client, a["public_id"])

    krishnanagar_body = _get_intel(client, "IN-WB-NADIA-KRISHNANAGAR")
    bbmp_body = _get_intel(client, "IN-KA-BLR-BBMP")
    assert krishnanagar_body["total_resolved"] == 1
    assert bbmp_body["total_resolved"] == 0


def test_invalid_jurisdiction_returns_404(client):
    response = client.get(
        "/api/admin/analytics/resolution-intelligence", params={"jurisdiction_code": "NOT-REAL"}
    )
    assert response.status_code == 404


# ============================================================================
# Authority authentication
# ============================================================================


def test_unauthenticated_request_rejected(real_auth_client):
    response = real_auth_client.get(
        "/api/admin/analytics/resolution-intelligence",
        params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"},
    )
    assert response.status_code == 401


def test_authenticated_request_succeeds(real_auth_client):
    login = real_auth_client.post(
        "/api/authority/login", json={"username": "m19_authority", "password": "m19-test-password"}
    )
    assert login.status_code == 200
    response = real_auth_client.get(
        "/api/admin/analytics/resolution-intelligence",
        params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"},
    )
    assert response.status_code == 200


# ============================================================================
# Reopened issues / stale / high-severity prioritization
# ============================================================================


def test_reopened_issue_triggers_insight(client):
    a = _submit_issue(client)
    _advance_and_resolve(client, a["public_id"])
    req = client.post(f"/api/track/{a['public_id']}/reopen-request", json={"reason": "Broke again."}).json()
    client.post(f"/api/admin/reopen-requests/{req['public_id']}/decision", json={"approve": True})

    insights = client.get(
        "/api/admin/insights", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    ).json()
    insight_types = {i["insight_type"] for i in insights["insights"]}
    assert "reopened_issues_attention" in insight_types


def test_no_reopened_issue_no_reopened_insight(client):
    _submit_issue(client)
    insights = client.get(
        "/api/admin/insights", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    ).json()
    insight_types = {i["insight_type"] for i in insights["insights"]}
    assert "reopened_issues_attention" not in insight_types


def test_high_severity_backlog_still_flagged(client):
    """Regression: the existing M10 high-severity insight must still
    fire correctly alongside the new M19 reopened-issue insight."""
    _submit_issue(client, severity=SeverityLevel.CRITICAL)
    _submit_issue(client, severity=SeverityLevel.HIGH)

    insights = client.get(
        "/api/admin/insights", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    ).json()
    insight_types = {i["insight_type"] for i in insights["insights"]}
    assert "high_severity_unresolved" in insight_types


# ============================================================================
# No issue/lifecycle mutation
# ============================================================================


def test_resolution_intelligence_never_mutates_issue_state(client):
    a = _submit_issue(client)
    _advance_and_resolve(client, a["public_id"])

    before = client.get(f"/api/admin/issues/{a['public_id']}").json()
    _get_intel(client)
    _get_intel(client)  # call twice -- must be fully idempotent
    after = client.get(f"/api/admin/issues/{a['public_id']}").json()
    assert before == after


def test_resolution_intelligence_never_calls_gemini(client):
    a = _submit_issue(client)
    _advance_and_resolve(client, a["public_id"])
    with patch("backend.service.analyze_complaint") as mock_analyze:
        _get_intel(client)
    mock_analyze.assert_not_called()


def test_resolution_intelligence_creates_no_history_record(client):
    a = _submit_issue(client)
    _advance_and_resolve(client, a["public_id"])
    before = len(client.get(f"/api/issues/{a['public_id']}/history").json())
    _get_intel(client)
    after = len(client.get(f"/api/issues/{a['public_id']}/history").json())
    assert after == before


# ============================================================================
# Analytics regression
# ============================================================================


def test_existing_dashboard_summary_still_works(client):
    _submit_issue(client)
    response = client.get(
        "/api/admin/dashboard/summary", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    )
    assert response.status_code == 200


def test_existing_resolution_timing_endpoint_still_works(client):
    a = _submit_issue(client)
    _advance_and_resolve(client, a["public_id"])
    response = client.get(
        "/api/admin/analytics/resolution-timing",
        params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"},
    )
    assert response.status_code == 200


def test_existing_insights_endpoint_still_works(client):
    _submit_issue(client)
    response = client.get(
        "/api/admin/insights", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    )
    assert response.status_code == 200
