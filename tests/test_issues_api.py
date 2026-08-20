"""
Tests for POST /api/issues and GET /api/issues/{public_id}.

analyze_complaint() is mocked in every test here -- no real Gemini calls.
Every test uses the `client` fixture below, which wires the FastAPI app to
an isolated, temporary SQLite database (never civicsync.db) via a get_db
dependency override, so persistence is genuinely exercised rather than
mocked away.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from google.genai.errors import APIError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from ai.schemas import CivicIssue, IssueCategory, SeverityLevel
from backend.database import Base, build_engine, get_db
from backend.main import app
from backend.models import IssueStatus
from backend.repository import get_issue_by_public_id


def _sample_civic_issue(**overrides) -> CivicIssue:
    base = dict(
        original_text="There has been no street light near our school for two weeks.",
        category=IssueCategory.STREET_LIGHTING,
        problem="Non-functional street light near a school.",
        location="Near the school",
        duration="Two weeks",
        severity=SeverityLevel.MEDIUM,
        affected_population="Students and pedestrians",
        suggested_department="Municipal Electrical Department",
        confidence=0.82,
    )
    base.update(overrides)
    return CivicIssue(**base)


@pytest.fixture
def client(tmp_path):
    """TestClient wired to an isolated, temporary SQLite database via a
    get_db dependency override -- never touches civicsync.db."""
    db_path = tmp_path / "test_issues_api.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

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


# --- POST /api/issues: happy path (exercises real persistence) -------------


def test_submit_issue_returns_201(client):
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()):
        response = client.post(
            "/api/issues",
            json={"text": "There has been no street light near our school for two weeks."},
        )
    assert response.status_code == 201


def test_submit_issue_response_contains_public_id(client):
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()):
        response = client.post("/api/issues", json={"text": "Some complaint."})
    body = response.json()
    assert "public_id" in body
    assert body["public_id"].startswith("CIV-")


def test_submit_issue_response_excludes_internal_id(client):
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()):
        response = client.post("/api/issues", json={"text": "Some complaint."})
    assert "id" not in response.json()


def test_submit_issue_starts_with_submitted_status(client):
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()):
        response = client.post("/api/issues", json={"text": "Some complaint."})
    assert response.json()["status"] == IssueStatus.SUBMITTED.value


def test_submit_issue_preserves_ai_suggested_department(client):
    civic_issue = _sample_civic_issue(suggested_department="Municipal Electrical Department")
    with patch("backend.service.analyze_complaint", return_value=civic_issue):
        response = client.post("/api/issues", json={"text": "Some complaint."})
    assert response.json()["suggested_department"] == "Municipal Electrical Department"


def test_submit_issue_assigned_department_is_null(client):
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()):
        response = client.post("/api/issues", json={"text": "Some complaint."})
    assert response.json()["assigned_department"] is None


def test_submit_issue_resolved_and_closed_timestamps_are_null(client):
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()):
        response = client.post("/api/issues", json={"text": "Some complaint."})
    body = response.json()
    assert body["resolved_at"] is None
    assert body["closed_at"] is None


def test_submit_issue_response_includes_all_public_fields(client):
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()):
        response = client.post("/api/issues", json={"text": "Some complaint."})
    body = response.json()
    assert set(body.keys()) == {
        "public_id",
        "original_text",
        "category",
        "problem",
        "location",
        "duration",
        "severity",
        "affected_population",
        "suggested_department",
        "confidence",
        "assigned_department",
        "status",
        "resolution_summary",
        "created_at",
        "updated_at",
        "resolved_at",
        "closed_at",
    }


# --- POST /api/issues: validation & error handling --------------------------


def test_submit_issue_rejects_blank_text(client):
    with patch("backend.service.analyze_complaint") as mock_analyze:
        response = client.post("/api/issues", json={"text": ""})
    assert response.status_code == 422
    mock_analyze.assert_not_called()


def test_submit_issue_handles_gemini_api_error(client):
    api_error = APIError(503, {"message": "internal detail that should not leak"})
    with patch("backend.service.analyze_complaint", side_effect=api_error):
        response = client.post("/api/issues", json={"text": "Some complaint."})
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail == "AI service is temporarily unavailable. Please try again shortly."
    assert "internal detail" not in detail


def test_submit_issue_handles_missing_api_key(client):
    secret_looking_message = "GEMINI_API_KEY is not set. sk-should-not-leak-12345"
    with patch(
        "backend.service.analyze_complaint",
        side_effect=EnvironmentError(secret_looking_message),
    ):
        response = client.post("/api/issues", json={"text": "Some complaint."})
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail == "AI service is not configured correctly."
    assert "sk-should-not-leak-12345" not in detail
    assert "GEMINI_API_KEY" not in detail


def test_submit_issue_handles_database_failure(client):
    db_error = SQLAlchemyError("connection to server at internal-db-host:5432 failed")
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()):
        with patch("backend.service.create_issue_from_civic_issue", side_effect=db_error):
            response = client.post("/api/issues", json={"text": "Some complaint."})
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail == "Failed to save the issue. Please try again."
    assert "internal-db-host" not in detail


def test_submit_issue_response_never_includes_stack_trace_text(client):
    with patch(
        "backend.service.analyze_complaint",
        side_effect=EnvironmentError("some config problem"),
    ):
        response = client.post("/api/issues", json={"text": "Some complaint."})
    body_text = response.text
    assert "Traceback" not in body_text
    assert "File \"" not in body_text


# --- GET /api/issues/{public_id} --------------------------------------------


def test_get_existing_issue_returns_200(client):
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()):
        created = client.post("/api/issues", json={"text": "Some complaint."}).json()

    response = client.get(f"/api/issues/{created['public_id']}")
    assert response.status_code == 200
    assert response.json()["public_id"] == created["public_id"]
    assert "id" not in response.json()


def test_get_nonexistent_issue_returns_404(client):
    response = client.get("/api/issues/CIV-DOES-NOT-EXIST")
    assert response.status_code == 404


def test_get_issue_does_not_call_gemini(client):
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()):
        created = client.post("/api/issues", json={"text": "Some complaint."}).json()

    with (
        patch("backend.main.analyze_complaint") as mock_main_analyze,
        patch("backend.service.analyze_complaint") as mock_service_analyze,
    ):
        response = client.get(f"/api/issues/{created['public_id']}")

    assert response.status_code == 200
    mock_main_analyze.assert_not_called()
    mock_service_analyze.assert_not_called()


# --- Persistence is genuinely exercised (not just mocked) -------------------


def test_issue_is_actually_persisted_and_retrievable_directly(client):
    """Beyond the API round trip above, query the same temp database
    directly through the repository layer to prove the row genuinely
    exists rather than just that the API echoed something plausible."""
    civic_issue = _sample_civic_issue(original_text="Direct persistence check complaint.")
    with patch("backend.service.analyze_complaint", return_value=civic_issue):
        created = client.post(
            "/api/issues", json={"text": "Direct persistence check complaint."}
        ).json()

    db_gen = app.dependency_overrides[get_db]()
    db = next(db_gen)
    try:
        issue = get_issue_by_public_id(db, created["public_id"])
        assert issue is not None
        assert issue.original_text == "Direct persistence check complaint."
        assert issue.status == IssueStatus.SUBMITTED
    finally:
        db.close()
