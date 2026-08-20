"""
API-level tests for backend/main.py using FastAPI's TestClient.

The AI service (ai.client.analyze_complaint) is mocked in every test here,
so these tests never call Gemini and never require a real GEMINI_API_KEY.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from google.genai.errors import APIError

from ai.schemas import CivicIssue, IssueCategory, SeverityLevel
from backend.main import app

client = TestClient(app)


def _sample_issue() -> CivicIssue:
    return CivicIssue(
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


# --- GET /health (must still work, unchanged) ------------------------------


def test_health_check_still_works():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "service": "CivicSync API",
        "version": "0.1.0",
    }


# --- POST /api/analyze: happy path -----------------------------------------


def test_analyze_returns_structured_civic_issue():
    issue = _sample_issue()

    with patch("backend.main.analyze_complaint", return_value=issue) as mock_analyze:
        response = client.post(
            "/api/analyze",
            json={"text": "There has been no street light near our school for two weeks."},
        )

    assert response.status_code == 200
    assert response.json() == issue.model_dump(mode="json")
    mock_analyze.assert_called_once_with(
        "There has been no street light near our school for two weeks."
    )


# --- POST /api/analyze: request validation ----------------------------------


def test_analyze_rejects_blank_text():
    with patch("backend.main.analyze_complaint") as mock_analyze:
        response = client.post("/api/analyze", json={"text": ""})

    assert response.status_code == 422
    mock_analyze.assert_not_called()


def test_analyze_rejects_missing_text_field():
    with patch("backend.main.analyze_complaint") as mock_analyze:
        response = client.post("/api/analyze", json={})

    assert response.status_code == 422
    mock_analyze.assert_not_called()


# --- POST /api/analyze: AI service error handling ---------------------------


def test_analyze_handles_value_error_as_bad_request():
    with patch("backend.main.analyze_complaint", side_effect=ValueError("bad input")):
        response = client.post("/api/analyze", json={"text": "Some complaint."})

    assert response.status_code == 400
    assert response.json()["detail"] == "bad input"


def test_analyze_handles_missing_api_key_without_leaking_details():
    secret_looking_message = "GEMINI_API_KEY is not set. sk-should-not-leak-12345"

    with patch(
        "backend.main.analyze_complaint",
        side_effect=EnvironmentError(secret_looking_message),
    ):
        response = client.post("/api/analyze", json={"text": "Some complaint."})

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail == "AI service is not configured correctly."
    assert "sk-should-not-leak-12345" not in detail
    assert "GEMINI_API_KEY" not in detail


def test_analyze_handles_gemini_api_error_as_bad_gateway():
    api_error = APIError(503, {"message": "internal detail that should not leak"})

    with patch("backend.main.analyze_complaint", side_effect=api_error):
        response = client.post("/api/analyze", json={"text": "Some complaint."})

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail == "AI service is temporarily unavailable. Please try again shortly."
    assert "internal detail" not in detail


def test_analyze_response_never_includes_stack_trace_text():
    with patch(
        "backend.main.analyze_complaint",
        side_effect=EnvironmentError("some config problem"),
    ):
        response = client.post("/api/analyze", json={"text": "Some complaint."})

    body_text = response.text
    assert "Traceback" not in body_text
    assert "File \"" not in body_text
