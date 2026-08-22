"""
Milestone 21: Citizen Geolocation Capture.

Backend/API tests only -- browser-only Geolocation API behavior
(permission prompts, getCurrentPosition callbacks) cannot be exercised
in a backend test and is instead verified via `node --check` on the
changed JS files plus a manual browser check (see the M21 report).

Reuses the established single-jurisdiction fixture pattern. Gemini is
mocked throughout -- no real Gemini calls.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from ai.schemas import CivicIssue, IssueCategory, SeverityLevel
from backend.auth import get_current_authority
from backend.database import Base, build_engine, get_db
from backend.main import app
from backend.models import Issue, Jurisdiction, JurisdictionLevel


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
def client(tmp_path):
    db_path = tmp_path / "test_m21.db"
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
    app.dependency_overrides[get_current_authority] = lambda: "test-authority"
    test_client = TestClient(app)
    test_client._Session = TestingSessionLocal
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _submit(client, **body):
    payload = {"text": "Deep pothole on the main road, very dangerous."}
    payload.update(body)
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()):
        return client.post("/api/issues", json=payload)


# ============================================================================
# Coordinates accepted / persisted
# ============================================================================


def test_coordinates_accepted(client):
    response = _submit(client, latitude=23.4058, longitude=88.4894, accuracy=15.5)
    assert response.status_code == 201


def test_coordinates_persisted(client):
    response = _submit(client, latitude=23.4058, longitude=88.4894, accuracy=15.5)
    public_id = response.json()["public_id"]

    db = client._Session()
    issue = db.query(Issue).filter(Issue.public_id == public_id).one()
    assert issue.latitude == 23.4058
    assert issue.longitude == 88.4894
    assert issue.location_accuracy == 15.5
    db.close()


def test_coordinates_visible_in_authority_response(client):
    response = _submit(client, latitude=23.4058, longitude=88.4894, accuracy=15.5)
    body = response.json()
    assert body["latitude"] == 23.4058
    assert body["longitude"] == 88.4894
    assert body["location_accuracy"] == 15.5


# ============================================================================
# Validation
# ============================================================================


@pytest.mark.parametrize("bad_latitude", [90.1, -90.1, 999, -999])
def test_latitude_validation_rejects_out_of_range(client, bad_latitude):
    response = _submit(client, latitude=bad_latitude, longitude=88.4894)
    assert response.status_code == 422


@pytest.mark.parametrize("bad_longitude", [180.1, -180.1, 999, -999])
def test_longitude_validation_rejects_out_of_range(client, bad_longitude):
    response = _submit(client, latitude=23.4058, longitude=bad_longitude)
    assert response.status_code == 422


def test_latitude_boundary_values_accepted(client):
    assert _submit(client, latitude=90, longitude=0).status_code == 201
    assert _submit(client, latitude=-90, longitude=0).status_code == 201


def test_longitude_boundary_values_accepted(client):
    assert _submit(client, latitude=0, longitude=180).status_code == 201
    assert _submit(client, latitude=0, longitude=-180).status_code == 201


def test_accuracy_validation_rejects_negative(client):
    response = _submit(client, latitude=23.4058, longitude=88.4894, accuracy=-1)
    assert response.status_code == 422


def test_accuracy_zero_accepted(client):
    response = _submit(client, latitude=23.4058, longitude=88.4894, accuracy=0)
    assert response.status_code == 201


def test_partial_coordinates_rejected(client):
    """latitude without longitude (or vice versa) must be rejected, not
    silently accepted with one side missing."""
    assert _submit(client, latitude=23.4058).status_code == 422
    assert _submit(client, longitude=88.4894).status_code == 422


def test_malformed_coordinate_type_rejected(client):
    response = client.post(
        "/api/issues", json={"text": "Some complaint.", "latitude": "not-a-number", "longitude": 88.4894}
    )
    assert response.status_code == 422


# ============================================================================
# Coordinates optional / existing submissions still work
# ============================================================================


def test_coordinates_optional_submission_without_them_succeeds(client):
    response = _submit(client)
    assert response.status_code == 201
    body = response.json()
    assert body["latitude"] is None
    assert body["longitude"] is None
    assert body["location_accuracy"] is None


def test_existing_submission_shape_unaffected_by_coordinates_being_absent(client):
    """Regression: a plain text-only submission behaves identically to
    pre-Milestone-21 behavior."""
    response = _submit(client)
    body = response.json()
    assert body["status"] == "SUBMITTED"
    assert body["category"] == "Roads and Potholes"


def test_accuracy_without_coordinates_rejected(client):
    """accuracy has no meaning without latitude/longitude -- supplying it
    alone must be rejected (422), not silently stored as an orphaned
    number with no associated location."""
    response = _submit(client, accuracy=10)
    assert response.status_code == 422


# ============================================================================
# Jurisdiction resolution remains correct
# ============================================================================


def test_jurisdiction_resolution_unaffected_by_coordinates(client):
    response = _submit(client, latitude=23.4058, longitude=88.4894)
    public_id = response.json()["public_id"]

    scoped = client.get(
        "/api/admin/issues", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    )
    ids = {i["public_id"] for i in scoped.json()["items"]}
    assert public_id in ids


# ============================================================================
# M20 integration
# ============================================================================


def test_m20_geo_issues_endpoint_receives_coordinate_bearing_issue(client):
    response = _submit(client, latitude=23.4058, longitude=88.4894, accuracy=10)
    public_id = response.json()["public_id"]

    geo = client.get(
        "/api/admin/analytics/geo-issues", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    )
    assert geo.status_code == 200
    geo_ids = {i["public_id"] for i in geo.json()}
    assert public_id in geo_ids


def test_m20_geo_issues_excludes_issue_without_coordinates(client):
    response = _submit(client)  # no coordinates
    public_id = response.json()["public_id"]

    geo = client.get(
        "/api/admin/analytics/geo-issues", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    )
    geo_ids = {i["public_id"] for i in geo.json()}
    assert public_id not in geo_ids


def test_m20_hotspot_detection_can_use_new_coordinates(client):
    """Three coordinate-bearing submissions close together must be
    detectable as a hotspot via the EXISTING M20 algorithm -- no
    duplicated/rewritten clustering logic."""
    base_lat, base_lng = 23.4058, 88.4894
    for i in range(3):
        _submit(client, latitude=base_lat + i * 0.0003, longitude=base_lng + i * 0.0003)

    hotspots = client.get(
        "/api/admin/analytics/hotspots", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    )
    assert hotspots.status_code == 200
    assert len(hotspots.json()["hotspots"]) == 1
    assert hotspots.json()["hotspots"][0]["complaint_count"] == 3


# ============================================================================
# Privacy: public tracking never exposes location
# ============================================================================


def test_public_tracking_does_not_expose_coordinates(client):
    response = _submit(client, latitude=23.4058, longitude=88.4894, accuracy=15.5)
    public_id = response.json()["public_id"]

    tracked = client.get(f"/api/track/{public_id}")
    assert tracked.status_code == 200
    body = tracked.json()
    assert "latitude" not in body
    assert "longitude" not in body
    assert "location_accuracy" not in body
    assert "23.4058" not in tracked.text


def test_public_tracking_unaffected_when_no_coordinates_submitted(client):
    response = _submit(client)
    public_id = response.json()["public_id"]
    tracked = client.get(f"/api/track/{public_id}")
    assert tracked.status_code == 200


# ============================================================================
# No Gemini call required / no lifecycle mutation
# ============================================================================


def test_coordinate_validation_failure_never_calls_gemini(client):
    with patch("backend.service.analyze_complaint") as mock_analyze:
        response = client.post(
            "/api/issues", json={"text": "Some complaint.", "latitude": 999, "longitude": 88.4894}
        )
    assert response.status_code == 422
    mock_analyze.assert_not_called()


def test_valid_coordinates_do_not_change_gemini_call_shape(client):
    """Coordinates are never passed to analyze_complaint -- Gemini only
    ever sees the complaint text, exactly as before Milestone 21."""
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()) as mock_analyze:
        client.post(
            "/api/issues",
            json={
                "text": "Deep pothole on the main road, very dangerous.",
                "latitude": 23.4058,
                "longitude": 88.4894,
            },
        )
    mock_analyze.assert_called_once_with("Deep pothole on the main road, very dangerous.")


def test_submission_with_coordinates_creates_no_extra_history_record(client):
    response = _submit(client, latitude=23.4058, longitude=88.4894)
    public_id = response.json()["public_id"]
    history = client.get(f"/api/issues/{public_id}/history")
    # Exactly the initial SUBMITTED row -- coordinates are not a
    # lifecycle event.
    assert len(history.json()) == 1
    assert history.json()[0]["to_status"] == "SUBMITTED"


def test_submission_with_coordinates_status_is_submitted_not_auto_advanced(client):
    response = _submit(client, latitude=23.4058, longitude=88.4894)
    assert response.json()["status"] == "SUBMITTED"


# ============================================================================
# Regression
# ============================================================================


def test_regression_normal_complaint_submission_unaffected(client):
    response = _submit(client)
    assert response.status_code == 201
    body = response.json()
    assert body["category"] == "Roads and Potholes"
    assert body["severity"] == "High"


def test_regression_admin_issue_detail_still_works_with_coordinates(client):
    response = _submit(client, latitude=23.4058, longitude=88.4894)
    public_id = response.json()["public_id"]
    detail = client.get(f"/api/admin/issues/{public_id}")
    assert detail.status_code == 200
    assert detail.json()["latitude"] == 23.4058
