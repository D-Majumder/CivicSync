"""
API-level tests for GET /api/departments, POST/GET
/api/issues/{public_id}/assignment, and GET
/api/issues/{public_id}/assignment/history.

analyze_complaint() is mocked (no Gemini calls anywhere in this file).
Every test uses the `client` fixture, which wires the FastAPI app to an
isolated, temporary SQLite database via a get_db dependency override --
never civicsync.db -- pre-seeded with two departments for assignment
tests.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from ai.schemas import CivicIssue, IssueCategory, SeverityLevel
from backend.database import Base, build_engine, get_db
from backend.auth import get_current_authority
from backend.main import app
from backend.models import Department


def _sample_civic_issue(**overrides) -> CivicIssue:
    base = dict(
        original_text="There has been no street light near our school for two weeks.",
        category=IssueCategory.STREET_LIGHTING,
        problem="Non-functional street light near a school.",
        severity=SeverityLevel.MEDIUM,
        confidence=0.8,
        suggested_department="Electrical / Street Lighting Department",
    )
    base.update(overrides)
    return CivicIssue(**base)


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "test_assignment_api.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    # Seed two active departments + one inactive, directly (not via the
    # Alembic migration -- this is a schema-only temp DB).
    seed_db = TestingSessionLocal()
    seed_db.add_all(
        [
            Department(code="STREET_LIGHTING", name="Street Lighting", is_active=True),
            Department(code="ELECTRICITY", name="Electricity", is_active=True),
            Department(code="LEGACY_DEPT", name="Legacy Department", is_active=False),
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


def _classify(client, public_id: str) -> None:
    response = client.post(f"/api/issues/{public_id}/status", json={"status": "CLASSIFIED"})
    assert response.status_code == 200


# --- 4. GET /api/departments --------------------------------------------------


def test_list_departments_returns_only_active(client):
    response = client.get("/api/departments")
    assert response.status_code == 200
    codes = {d["code"] for d in response.json()}
    assert codes == {"STREET_LIGHTING", "ELECTRICITY"}


def test_list_departments_response_shape(client):
    response = client.get("/api/departments")
    for department in response.json():
        assert set(department.keys()) == {"code", "name", "description", "is_active"}
        assert "id" not in department


def test_list_departments_does_not_call_gemini(client):
    with (
        patch("backend.main.analyze_complaint") as mock_main,
        patch("backend.service.analyze_complaint") as mock_service,
    ):
        response = client.get("/api/departments")
    assert response.status_code == 200
    mock_main.assert_not_called()
    mock_service.assert_not_called()


# --- 5, 6, 7: successful assignment -------------------------------------------


def test_assign_issue_succeeds(client):
    created = _submit_issue(client)
    _classify(client, created["public_id"])

    response = client.post(
        f"/api/issues/{created['public_id']}/assignment",
        json={"department_code": "STREET_LIGHTING", "reason": "Routed."},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["assigned_department"] == {"code": "STREET_LIGHTING", "name": "Street Lighting"}


def test_assign_issue_suggested_department_unchanged(client):
    created = _submit_issue(client)
    _classify(client, created["public_id"])

    response = client.post(
        f"/api/issues/{created['public_id']}/assignment",
        json={"department_code": "ELECTRICITY"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["suggested_department"] == "Electrical / Street Lighting Department"
    assert body["assigned_department"]["code"] == "ELECTRICITY"


def test_assign_issue_response_excludes_internal_ids(client):
    created = _submit_issue(client)
    _classify(client, created["public_id"])

    response = client.post(
        f"/api/issues/{created['public_id']}/assignment",
        json={"department_code": "STREET_LIGHTING"},
    )
    body = response.json()
    assert "id" not in body
    assert "assigned_department_id" not in body
    assert "id" not in body["assigned_department"]


# --- 8. no Gemini calls --------------------------------------------------------


def test_assign_issue_does_not_call_gemini(client):
    created = _submit_issue(client)
    _classify(client, created["public_id"])

    with (
        patch("backend.main.analyze_complaint") as mock_main,
        patch("backend.service.analyze_complaint") as mock_service,
    ):
        response = client.post(
            f"/api/issues/{created['public_id']}/assignment",
            json={"department_code": "STREET_LIGHTING"},
        )
    assert response.status_code == 200
    mock_main.assert_not_called()
    mock_service.assert_not_called()


# --- 9. CLASSIFIED + assignment -> ROUTED -------------------------------------


def test_assign_issue_transitions_classified_to_routed(client):
    created = _submit_issue(client)
    _classify(client, created["public_id"])

    response = client.post(
        f"/api/issues/{created['public_id']}/assignment",
        json={"department_code": "STREET_LIGHTING"},
    )
    assert response.json()["status"] == "ROUTED"


# --- 10. reassignment does not reset lifecycle --------------------------------


def test_reassignment_of_routed_issue_does_not_reset_status(client):
    created = _submit_issue(client)
    _classify(client, created["public_id"])
    client.post(
        f"/api/issues/{created['public_id']}/assignment",
        json={"department_code": "STREET_LIGHTING"},
    )

    response = client.post(
        f"/api/issues/{created['public_id']}/assignment",
        json={"department_code": "ELECTRICITY", "reason": "Reassigned."},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ROUTED"
    assert response.json()["assigned_department"]["code"] == "ELECTRICITY"


# --- 14. GET current assignment -----------------------------------------------


def test_get_current_assignment_returns_null_when_never_assigned(client):
    created = _submit_issue(client)
    response = client.get(f"/api/issues/{created['public_id']}/assignment")
    assert response.status_code == 200
    assert response.json() is None


def test_get_current_assignment_after_assignment(client):
    created = _submit_issue(client)
    _classify(client, created["public_id"])
    client.post(
        f"/api/issues/{created['public_id']}/assignment",
        json={"department_code": "STREET_LIGHTING", "reason": "Initial routing."},
    )

    response = client.get(f"/api/issues/{created['public_id']}/assignment")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "STREET_LIGHTING"
    assert body["name"] == "Street Lighting"
    assert body["reason"] == "Initial routing."
    assert body["assigned_at"] is not None


def test_get_current_assignment_missing_issue_returns_404(client):
    response = client.get("/api/issues/CIV-DOES-NOT-EXIST/assignment")
    assert response.status_code == 404


def test_get_current_assignment_does_not_call_gemini(client):
    created = _submit_issue(client)
    with (
        patch("backend.main.analyze_complaint") as mock_main,
        patch("backend.service.analyze_complaint") as mock_service,
    ):
        response = client.get(f"/api/issues/{created['public_id']}/assignment")
    assert response.status_code == 200
    mock_main.assert_not_called()
    mock_service.assert_not_called()


# --- 15. GET assignment history ------------------------------------------------


def test_get_assignment_history_returns_entries(client):
    created = _submit_issue(client)
    _classify(client, created["public_id"])
    client.post(
        f"/api/issues/{created['public_id']}/assignment",
        json={"department_code": "STREET_LIGHTING", "reason": "Initial routing."},
    )
    client.post(
        f"/api/issues/{created['public_id']}/assignment",
        json={"department_code": "ELECTRICITY", "reason": "Reassigned."},
    )

    response = client.get(f"/api/issues/{created['public_id']}/assignment/history")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert body[0]["department_code"] == "STREET_LIGHTING"
    assert body[0]["unassigned_at"] is not None
    assert body[1]["department_code"] == "ELECTRICITY"
    assert body[1]["unassigned_at"] is None


def test_get_assignment_history_excludes_internal_ids(client):
    created = _submit_issue(client)
    _classify(client, created["public_id"])
    client.post(
        f"/api/issues/{created['public_id']}/assignment",
        json={"department_code": "STREET_LIGHTING"},
    )
    response = client.get(f"/api/issues/{created['public_id']}/assignment/history")
    for entry in response.json():
        assert "id" not in entry
        assert "issue_id" not in entry
        assert "department_id" not in entry


def test_get_assignment_history_missing_issue_returns_404(client):
    response = client.get("/api/issues/CIV-DOES-NOT-EXIST/assignment/history")
    assert response.status_code == 404


# --- 16, 17: invalid / inactive department ------------------------------------


def test_assign_issue_invalid_department_code_returns_404(client):
    created = _submit_issue(client)
    _classify(client, created["public_id"])

    response = client.post(
        f"/api/issues/{created['public_id']}/assignment",
        json={"department_code": "NOT_A_REAL_DEPARTMENT"},
    )
    assert response.status_code == 404


def test_assign_issue_inactive_department_returns_400(client):
    created = _submit_issue(client)
    _classify(client, created["public_id"])

    response = client.post(
        f"/api/issues/{created['public_id']}/assignment",
        json={"department_code": "LEGACY_DEPT"},
    )
    assert response.status_code == 400


# --- 18. missing issue ---------------------------------------------------------


def test_assign_issue_missing_issue_returns_404(client):
    response = client.post(
        "/api/issues/CIV-DOES-NOT-EXIST/assignment",
        json={"department_code": "STREET_LIGHTING"},
    )
    assert response.status_code == 404


# --- SUBMITTED must be classified first (section 10 of the spec) -----------


def test_assign_submitted_issue_returns_400(client):
    created = _submit_issue(client)  # still SUBMITTED, not classified
    response = client.post(
        f"/api/issues/{created['public_id']}/assignment",
        json={"department_code": "STREET_LIGHTING"},
    )
    assert response.status_code == 400
    assert "classif" in response.json()["detail"].lower()


# --- 22. failed assignment does not leave partial state -----------------------


def test_failed_assignment_does_not_change_issue(client):
    created = _submit_issue(client)
    _classify(client, created["public_id"])

    with patch(
        "backend.service.assign_department_to_issue",
        side_effect=__import__("sqlalchemy").exc.SQLAlchemyError("simulated db failure"),
    ):
        response = client.post(
            f"/api/issues/{created['public_id']}/assignment",
            json={"department_code": "STREET_LIGHTING"},
        )
    assert response.status_code == 500
    assert "simulated db failure" not in response.text

    refetched = client.get(f"/api/issues/{created['public_id']}").json()
    assert refetched["status"] == "CLASSIFIED"
    assert refetched["assigned_department"] is None


# --- No leakage of internal details, stack traces, or credentials -----------


def test_assignment_errors_never_leak_stack_trace_text(client):
    created = _submit_issue(client)
    response = client.post(
        f"/api/issues/{created['public_id']}/assignment",
        json={"department_code": "STREET_LIGHTING"},
    )
    assert response.status_code == 400
    assert "Traceback" not in response.text
    assert "File \"" not in response.text


# --- Existing endpoints unaffected --------------------------------------------


def test_existing_get_issue_includes_null_assigned_department_before_assignment(client):
    created = _submit_issue(client)
    response = client.get(f"/api/issues/{created['public_id']}")
    assert response.status_code == 200
    assert response.json()["assigned_department"] is None
