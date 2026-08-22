"""
API-level tests for POST /api/issues/{public_id}/status and
GET /api/issues/{public_id}/history.

analyze_complaint() is mocked (no Gemini calls anywhere in this file).
Every test uses the `client` fixture, which wires the FastAPI app to an
isolated, temporary SQLite database via a get_db dependency override --
never civicsync.db.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from ai.schemas import CivicIssue, IssueCategory, SeverityLevel
from backend.database import Base, build_engine, get_db
from backend.auth import get_current_authority
from backend.main import app
from backend.models import Jurisdiction, JurisdictionLevel


def _sample_civic_issue(**overrides) -> CivicIssue:
    base = dict(
        original_text="There has been no street light near our school for two weeks.",
        category=IssueCategory.STREET_LIGHTING,
        problem="Non-functional street light near a school.",
        severity=SeverityLevel.MEDIUM,
        confidence=0.8,
    )
    base.update(overrides)
    return CivicIssue(**base)


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "test_lifecycle_api.db"
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
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _submit_issue(client) -> dict:
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()):
        response = client.post("/api/issues", json={"text": "Some complaint."})
    assert response.status_code == 201
    return response.json()


# --- POST /api/issues/{public_id}/status -------------------------------------


def test_status_transition_succeeds(client):
    created = _submit_issue(client)
    response = client.post(
        f"/api/issues/{created['public_id']}/status",
        json={"status": "CLASSIFIED", "reason": "Validated civic issue"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "CLASSIFIED"


def test_status_transition_response_excludes_internal_id(client):
    created = _submit_issue(client)
    response = client.post(
        f"/api/issues/{created['public_id']}/status", json={"status": "CLASSIFIED"}
    )
    assert "id" not in response.json()


def test_status_transition_missing_issue_returns_404(client):
    response = client.post(
        "/api/issues/CIV-DOES-NOT-EXIST/status", json={"status": "CLASSIFIED"}
    )
    assert response.status_code == 404


def test_invalid_transition_returns_400(client):
    created = _submit_issue(client)
    response = client.post(
        f"/api/issues/{created['public_id']}/status", json={"status": "RESOLVED"}
    )
    assert response.status_code == 400


def test_invalid_transition_leaves_issue_unchanged(client):
    created = _submit_issue(client)
    client.post(f"/api/issues/{created['public_id']}/status", json={"status": "RESOLVED"})
    refetched = client.get(f"/api/issues/{created['public_id']}").json()
    assert refetched["status"] == "SUBMITTED"


def test_invalid_status_value_returns_422(client):
    created = _submit_issue(client)
    response = client.post(
        f"/api/issues/{created['public_id']}/status",
        json={"status": "NOT_A_REAL_STATUS"},
    )
    assert response.status_code == 422


def test_status_transition_does_not_call_gemini(client):
    created = _submit_issue(client)
    with (
        patch("backend.main.analyze_complaint") as mock_main,
        patch("backend.service.analyze_complaint") as mock_service,
    ):
        response = client.post(
            f"/api/issues/{created['public_id']}/status", json={"status": "CLASSIFIED"}
        )
    assert response.status_code == 200
    mock_main.assert_not_called()
    mock_service.assert_not_called()


# --- GET /api/issues/{public_id}/history --------------------------------------


def test_get_history_returns_entries(client):
    created = _submit_issue(client)
    client.post(
        f"/api/issues/{created['public_id']}/status",
        json={"status": "CLASSIFIED", "reason": "Validated civic issue"},
    )
    response = client.get(f"/api/issues/{created['public_id']}/history")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert body[0]["from_status"] is None
    assert body[0]["to_status"] == "SUBMITTED"
    assert body[0]["reason"] == "Issue submitted."
    assert body[1]["from_status"] == "SUBMITTED"
    assert body[1]["to_status"] == "CLASSIFIED"
    assert body[1]["reason"] == "Validated civic issue"


def test_get_history_excludes_internal_ids(client):
    created = _submit_issue(client)
    response = client.get(f"/api/issues/{created['public_id']}/history")
    for entry in response.json():
        assert "id" not in entry
        assert "issue_id" not in entry


def test_get_history_missing_issue_returns_404(client):
    response = client.get("/api/issues/CIV-DOES-NOT-EXIST/history")
    assert response.status_code == 404


def test_get_history_does_not_call_gemini(client):
    created = _submit_issue(client)
    with (
        patch("backend.main.analyze_complaint") as mock_main,
        patch("backend.service.analyze_complaint") as mock_service,
    ):
        response = client.get(f"/api/issues/{created['public_id']}/history")
    assert response.status_code == 200
    mock_main.assert_not_called()
    mock_service.assert_not_called()


# --- Full lifecycle round trip via the API -----------------------------------


def test_full_lifecycle_round_trip(client):
    created = _submit_issue(client)
    public_id = created["public_id"]

    for target in (
        "CLASSIFIED",
        "ROUTED",
        "ACKNOWLEDGED",
        "IN_PROGRESS",
        "RESOLVED",
        "CLOSED",
    ):
        response = client.post(f"/api/issues/{public_id}/status", json={"status": target})
        assert response.status_code == 200, response.json()
        assert response.json()["status"] == target

    history = client.get(f"/api/issues/{public_id}/history").json()
    assert [entry["to_status"] for entry in history] == [
        "SUBMITTED",
        "CLASSIFIED",
        "ROUTED",
        "ACKNOWLEDGED",
        "IN_PROGRESS",
        "RESOLVED",
        "CLOSED",
    ]

    final = client.get(f"/api/issues/{public_id}").json()
    assert final["resolved_at"] is not None
    assert final["closed_at"] is not None


def test_reject_and_reopen_paths_via_api(client):
    created = _submit_issue(client)
    public_id = created["public_id"]

    reject_response = client.post(
        f"/api/issues/{public_id}/status", json={"status": "REJECTED"}
    )
    assert reject_response.status_code == 200
    assert reject_response.json()["status"] == "REJECTED"

    # REJECTED is terminal -- nothing should be allowed out of it.
    dead_end = client.post(
        f"/api/issues/{public_id}/status", json={"status": "CLASSIFIED"}
    )
    assert dead_end.status_code == 400


# --- Regression: existing endpoints unaffected --------------------------------


def test_existing_post_issues_still_creates_submitted(client):
    created = _submit_issue(client)
    assert created["status"] == "SUBMITTED"
    assert created["assigned_department"] is None
    assert created["resolved_at"] is None
    assert created["closed_at"] is None


def test_existing_get_issue_still_works(client):
    created = _submit_issue(client)
    response = client.get(f"/api/issues/{created['public_id']}")
    assert response.status_code == 200
    assert response.json()["public_id"] == created["public_id"]
