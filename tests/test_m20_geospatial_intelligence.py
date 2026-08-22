"""
Milestone 20: AI + Geospatial Civic Intelligence.

Two layers of tests:
1. Pure, DB-free tests of backend/hotspots.py's deterministic clustering
   (no database, no app, no fixtures -- exactly the point of keeping
   detection logic separate from persistence).
2. API-level tests reusing the established two-jurisdiction fixture
   pattern from tests/test_m19_operational_intelligence.py (Krishnanagar
   / Bengaluru), since issues are created directly with coordinates set
   (no citizen-facing capture flow exists yet to submit through).

Gemini is mocked throughout at the API layer -- no real Gemini calls.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from ai.schemas import CivicIssue, IssueCategory, SeverityLevel
from backend.auth import get_current_authority, hash_password
from backend.database import Base, build_engine, get_db
from backend.hotspots import (
    HOTSPOT_MIN_COUNT,
    HOTSPOT_RADIUS_KM,
    GeoIssuePoint,
    detect_hotspots,
    haversine_km,
)
from backend.main import app
from backend.models import Department, Issue, IssueStatus, Jurisdiction, JurisdictionLevel

KRISHNANAGAR_LAT, KRISHNANAGAR_LNG = 23.4058, 88.4894  # real Krishnanagar, WB coordinates


def _point(public_id, lat, lng, category="Roads and Potholes", severity="High", status="SUBMITTED", days_ago=0):
    return GeoIssuePoint(
        public_id=public_id,
        latitude=lat,
        longitude=lng,
        category=category,
        severity=severity,
        status=status,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


# ============================================================================
# Pure, DB-free hotspot detection tests
# ============================================================================


def test_geographically_close_complaints_form_a_hotspot():
    points = [
        _point("CIV-A", KRISHNANAGAR_LAT, KRISHNANAGAR_LNG),
        _point("CIV-B", KRISHNANAGAR_LAT + 0.001, KRISHNANAGAR_LNG + 0.001),
        _point("CIV-C", KRISHNANAGAR_LAT + 0.0005, KRISHNANAGAR_LNG + 0.0005),
    ]
    hotspots = detect_hotspots(points)
    assert len(hotspots) == 1
    assert hotspots[0].complaint_count == 3
    assert set(hotspots[0].member_public_ids) == {"CIV-A", "CIV-B", "CIV-C"}


def test_distant_complaints_do_not_form_a_hotspot():
    points = [
        _point("CIV-A", KRISHNANAGAR_LAT, KRISHNANAGAR_LNG),
        _point("CIV-B", 23.5000, 87.3000),  # Nadia district, but ~130km away
        _point("CIV-C", 12.9716, 77.5946),  # Bengaluru, ~1700km away
    ]
    hotspots = detect_hotspots(points)
    assert hotspots == []


def test_below_min_count_does_not_form_a_hotspot():
    """Two close points is not enough -- HOTSPOT_MIN_COUNT (3) is required."""
    points = [
        _point("CIV-A", KRISHNANAGAR_LAT, KRISHNANAGAR_LNG),
        _point("CIV-B", KRISHNANAGAR_LAT + 0.0005, KRISHNANAGAR_LNG + 0.0005),
    ]
    assert len(points) < HOTSPOT_MIN_COUNT
    hotspots = detect_hotspots(points)
    assert hotspots == []


def test_unrelated_categories_still_cluster_geographically():
    """Category is never a clustering criterion -- close points of
    DIFFERENT categories still form one hotspot, with the mix correctly
    reported rather than causing a crash or a forced split."""
    points = [
        _point("CIV-A", KRISHNANAGAR_LAT, KRISHNANAGAR_LNG, category="Roads and Potholes"),
        _point("CIV-B", KRISHNANAGAR_LAT + 0.0005, KRISHNANAGAR_LNG + 0.0005, category="Street Lighting"),
        _point("CIV-C", KRISHNANAGAR_LAT + 0.0003, KRISHNANAGAR_LNG + 0.0003, category="Water Supply"),
    ]
    hotspots = detect_hotspots(points)
    assert len(hotspots) == 1
    assert hotspots[0].complaint_count == 3
    assert hotspots[0].category_breakdown == {
        "Roads and Potholes": 1, "Street Lighting": 1, "Water Supply": 1,
    }
    # Tie-broken deterministically among equally-common categories --
    # max() by (count, name) picks the alphabetically-LAST name.
    assert hotspots[0].dominant_category == "Water Supply"


def test_severity_aggregation():
    points = [
        _point("CIV-A", KRISHNANAGAR_LAT, KRISHNANAGAR_LNG, severity="Critical"),
        _point("CIV-B", KRISHNANAGAR_LAT + 0.0003, KRISHNANAGAR_LNG + 0.0003, severity="High"),
        _point("CIV-C", KRISHNANAGAR_LAT + 0.0004, KRISHNANAGAR_LNG + 0.0004, severity="High"),
    ]
    hotspots = detect_hotspots(points)
    assert hotspots[0].severity_breakdown == {"Critical": 1, "High": 2}
    assert hotspots[0].priority_signal == "HIGH"  # Critical present


def test_status_aggregation():
    points = [
        _point("CIV-A", KRISHNANAGAR_LAT, KRISHNANAGAR_LNG, status="Submitted"),
        _point("CIV-B", KRISHNANAGAR_LAT + 0.0003, KRISHNANAGAR_LNG + 0.0003, status="Routed"),
        _point("CIV-C", KRISHNANAGAR_LAT + 0.0004, KRISHNANAGAR_LNG + 0.0004, status="Routed"),
    ]
    hotspots = detect_hotspots(points)
    assert hotspots[0].status_breakdown == {"Submitted": 1, "Routed": 2}


def test_hotspot_center_calculation():
    points = [
        _point("CIV-A", 23.0000, 88.0000),
        _point("CIV-B", 23.0010, 88.0000),
        _point("CIV-C", 23.0005, 88.0000),
    ]
    hotspots = detect_hotspots(points)
    assert hotspots[0].center_latitude == pytest.approx(23.0005, abs=1e-6)
    assert hotspots[0].center_longitude == pytest.approx(88.0000, abs=1e-6)


def test_no_data_behavior():
    assert detect_hotspots([]) == []


def test_deterministic_repeatable_results():
    points = [
        _point("CIV-C", KRISHNANAGAR_LAT, KRISHNANAGAR_LNG),
        _point("CIV-A", KRISHNANAGAR_LAT + 0.0003, KRISHNANAGAR_LNG + 0.0003),
        _point("CIV-B", KRISHNANAGAR_LAT + 0.0004, KRISHNANAGAR_LNG + 0.0004),
        _point("CIV-D", 23.5000, 87.3000),
    ]
    first = detect_hotspots(points)
    second = detect_hotspots(list(reversed(points)))
    third = detect_hotspots(sorted(points, key=lambda p: p.public_id))
    assert first == second == third


def test_haversine_known_distance():
    """Sanity-check the distance function against a real, well-known
    distance: Kolkata to Delhi is approximately 1300-1500km."""
    kolkata = (22.5726, 88.3639)
    delhi = (28.6139, 77.2090)
    distance = haversine_km(*kolkata, *delhi)
    assert 1300 < distance < 1500


def test_haversine_zero_distance_for_same_point():
    assert haversine_km(23.0, 88.0, 23.0, 88.0) == pytest.approx(0.0, abs=1e-9)


def test_radius_boundary_respected():
    """A point exactly outside HOTSPOT_RADIUS_KM of every cluster member
    must not join it."""
    # ~0.5km north of the origin is roughly 0.0045 degrees latitude.
    just_inside = _point("CIV-B", KRISHNANAGAR_LAT + 0.003, KRISHNANAGAR_LNG)
    just_outside = _point("CIV-D", KRISHNANAGAR_LAT + 0.02, KRISHNANAGAR_LNG)  # ~2.2km away
    origin = _point("CIV-A", KRISHNANAGAR_LAT, KRISHNANAGAR_LNG)
    third = _point("CIV-C", KRISHNANAGAR_LAT + 0.001, KRISHNANAGAR_LNG)

    dist_outside = haversine_km(
        origin.latitude, origin.longitude, just_outside.latitude, just_outside.longitude
    )
    assert dist_outside > HOTSPOT_RADIUS_KM

    hotspots = detect_hotspots([origin, just_inside, third, just_outside])
    assert len(hotspots) == 1
    assert "CIV-D" not in hotspots[0].member_public_ids


# ============================================================================
# API-level tests (jurisdiction isolation, auth, no-mutation, regression)
# ============================================================================


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


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CIVICSYNC_EVIDENCE_STORAGE_DIR", str(tmp_path / "evidence_storage"))
    db_path = tmp_path / "test_m20.db"
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
    seed_db.add(dept_a)
    seed_db.commit()
    krishnanagar_id = krishnanagar.id
    bbmp_id = bbmp.id
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
    test_client._Session = TestingSessionLocal  # exposed for direct DB seeding below
    test_client._krishnanagar_id = krishnanagar_id
    test_client._bbmp_id = bbmp_id
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


@pytest.fixture
def real_auth_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTHORITY_USERNAME", "m20_authority")
    monkeypatch.setenv("AUTHORITY_PASSWORD_HASH", hash_password("m20-test-password"))
    monkeypatch.setenv("SESSION_SECRET", "m20-test-secret")
    db_path = tmp_path / "test_m20_auth.db"
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


def _set_coordinates(client, public_id, lat, lng):
    """Directly sets coordinates via the DB session -- no citizen-facing
    capture flow exists yet to do this through the API."""
    db = client._Session()
    issue = db.query(Issue).filter(Issue.public_id == public_id).one()
    issue.latitude = lat
    issue.longitude = lng
    db.commit()
    db.close()


def _seed_cluster(client, jurisdiction_id, count=3, base_lat=KRISHNANAGAR_LAT, base_lng=KRISHNANAGAR_LNG):
    created = []
    for i in range(count):
        issue = _submit_issue(client)
        _set_coordinates(client, issue["public_id"], base_lat + i * 0.0003, base_lng + i * 0.0003)
        created.append(issue)
    return created


def _get_hotspots(client, jurisdiction_code="IN-WB-NADIA-KRISHNANAGAR"):
    response = client.get(
        "/api/admin/analytics/hotspots", params={"jurisdiction_code": jurisdiction_code}
    )
    assert response.status_code == 200
    return response.json()


def test_api_hotspot_detected_from_seeded_cluster(client):
    _seed_cluster(client, client._krishnanagar_id)
    body = _get_hotspots(client)
    assert body["geo_tagged_issue_count"] == 3
    assert len(body["hotspots"]) == 1
    assert body["hotspots"][0]["complaint_count"] == 3


def test_no_hotspots_when_no_coordinates_set(client):
    _submit_issue(client)  # no coordinates set at all
    body = _get_hotspots(client)
    assert body["geo_tagged_issue_count"] == 0
    assert body["hotspots"] == []


def test_hotspot_response_excludes_internal_ids(client):
    _seed_cluster(client, client._krishnanagar_id)
    body = _get_hotspots(client)
    hotspot = body["hotspots"][0]
    assert "id" not in hotspot
    for member_id in hotspot["member_public_ids"]:
        assert member_id.startswith("CIV-")


def test_jurisdiction_isolation_hotspots(client):
    _seed_cluster(client, client._krishnanagar_id)  # Krishnanagar coordinates
    krishnanagar_body = _get_hotspots(client, "IN-WB-NADIA-KRISHNANAGAR")
    bbmp_body = _get_hotspots(client, "IN-KA-BLR-BBMP")
    assert len(krishnanagar_body["hotspots"]) == 1
    assert len(bbmp_body["hotspots"]) == 0
    assert bbmp_body["geo_tagged_issue_count"] == 0


def test_invalid_jurisdiction_returns_404(client):
    response = client.get(
        "/api/admin/analytics/hotspots", params={"jurisdiction_code": "NOT-REAL"}
    )
    assert response.status_code == 404


def test_geo_issues_endpoint_invalid_jurisdiction_returns_404(client):
    response = client.get(
        "/api/admin/analytics/geo-issues", params={"jurisdiction_code": "NOT-REAL"}
    )
    assert response.status_code == 404


def test_unauthenticated_hotspots_request_rejected(real_auth_client):
    response = real_auth_client.get(
        "/api/admin/analytics/hotspots", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    )
    assert response.status_code == 401


def test_authenticated_hotspots_request_succeeds(real_auth_client):
    login = real_auth_client.post(
        "/api/authority/login", json={"username": "m20_authority", "password": "m20-test-password"}
    )
    assert login.status_code == 200
    response = real_auth_client.get(
        "/api/admin/analytics/hotspots", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    )
    assert response.status_code == 200


def test_hotspots_endpoint_never_mutates_issue_state(client):
    created = _seed_cluster(client, client._krishnanagar_id)
    before = client.get(f"/api/admin/issues/{created[0]['public_id']}").json()
    _get_hotspots(client)
    _get_hotspots(client)  # call twice -- must be fully idempotent
    after = client.get(f"/api/admin/issues/{created[0]['public_id']}").json()
    assert before == after


def test_hotspot_detection_never_calls_gemini(client):
    _seed_cluster(client, client._krishnanagar_id)
    with patch("backend.service.analyze_complaint") as mock_analyze:
        _get_hotspots(client)
    mock_analyze.assert_not_called()


def test_hotspot_wrapped_into_existing_insights_endpoint(client):
    """Confirms hotspots flow through the EXISTING deterministic insight
    system (Milestone 10) rather than a parallel one -- no new AI
    endpoint was created."""
    _seed_cluster(client, client._krishnanagar_id)
    insights = client.get(
        "/api/admin/insights", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    ).json()
    insight_types = {i["insight_type"] for i in insights["insights"]}
    assert "civic_hotspot" in insight_types


def test_existing_insights_regression_high_severity_still_works(client):
    _submit_issue(client, severity=SeverityLevel.CRITICAL)
    _submit_issue(client, severity=SeverityLevel.HIGH)
    insights = client.get(
        "/api/admin/insights", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    ).json()
    insight_types = {i["insight_type"] for i in insights["insights"]}
    assert "high_severity_unresolved" in insight_types


def test_existing_resolution_intelligence_endpoint_still_works(client):
    _submit_issue(client)
    response = client.get(
        "/api/admin/analytics/resolution-intelligence",
        params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"},
    )
    assert response.status_code == 200


def test_existing_dashboard_summary_still_works(client):
    _submit_issue(client)
    response = client.get(
        "/api/admin/dashboard/summary", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    )
    assert response.status_code == 200
