"""
Milestone 14, Priority 2: on-demand "Explain with AI" authority capability
(POST /api/admin/issues/{public_id}/explain).

Gemini is mocked in every test in this file, matching the existing
established pattern in tests/test_insights_api.py -- no real Gemini
calls. Every test uses an isolated, temporary SQLite database via a
get_db dependency override -- never civicsync.db.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from google.genai.errors import APIError
from sqlalchemy.orm import sessionmaker

from ai.schemas import CivicIssue, IssueCategory, IssueExplanationOutput, SeverityLevel
from backend.database import Base, build_engine, get_db
from backend.auth import get_current_authority
from backend.main import app
from backend.models import Department


def _sample_civic_issue(**overrides) -> CivicIssue:
    base = dict(
        original_text="There has been no street light near our school for two weeks.",
        category=IssueCategory.STREET_LIGHTING,
        problem="Lack of functioning street light near a school.",
        location="Near the school",
        severity=SeverityLevel.HIGH,
        confidence=0.88,
        suggested_department="Electrical / Street Lighting Department",
    )
    base.update(overrides)
    return CivicIssue(**base)


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "test_issue_explanation.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    seed_db = TestingSessionLocal()
    seed_db.add(Department(code="STREET_LIGHTING", name="Street Lighting", is_active=True))
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


def _submit_issue(client, **overrides) -> dict:
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue(**overrides)):
        response = client.post("/api/issues", json={"text": "Some complaint text."})
    assert response.status_code == 201
    return response.json()


# --- Gemini success ------------------------------------------------------------


def test_explain_gemini_success_path(client):
    created = _submit_issue(client)
    fake_output = IssueExplanationOutput(
        explanation="This is a street lighting issue near a school, correctly flagged High severity given the safety implications for children.",
        considerations=["Proximity to a school", "Two-week duration"],
    )
    with patch("backend.service.generate_issue_explanation", return_value=fake_output):
        response = client.post(f"/api/admin/issues/{created['public_id']}/explain")
    assert response.status_code == 200
    body = response.json()
    assert "school" in body["explanation"]
    assert body["considerations"] == ["Proximity to a school", "Two-week duration"]
    assert body["error"] is None


def test_explain_sends_only_structured_fields_never_raw_text_or_public_id(client):
    """Privacy discipline check: the payload sent to Gemini must never
    include original_text or public_id, matching the same rule already
    enforced for insight prioritization."""
    created = _submit_issue(client)
    fake_output = IssueExplanationOutput(explanation="Reasonable.", considerations=[])
    captured = {}

    def _capture(issue_context):
        captured.update(issue_context)
        return fake_output

    with patch("backend.service.generate_issue_explanation", side_effect=_capture):
        response = client.post(f"/api/admin/issues/{created['public_id']}/explain")
    assert response.status_code == 200
    assert "original_text" not in captured
    assert "public_id" not in captured
    assert captured["category"] == "Street Lighting"
    assert captured["severity"] == "High"
    assert captured["status_label"] == "Submitted"


# --- Gemini failure / graceful degradation --------------------------------------


def test_explain_gemini_environment_error_degrades_gracefully(client):
    created = _submit_issue(client)
    with patch(
        "backend.service.generate_issue_explanation",
        side_effect=EnvironmentError("GEMINI_API_KEY is not set. secret-looking-value"),
    ):
        response = client.post(f"/api/admin/issues/{created['public_id']}/explain")
    assert response.status_code == 200
    body = response.json()
    assert body["explanation"] is None
    assert body["considerations"] == []
    assert body["error"] == "AI explanation is temporarily unavailable."
    assert "secret-looking-value" not in response.text
    assert "GEMINI_API_KEY" not in response.text


def test_explain_gemini_api_error_degrades_gracefully(client):
    created = _submit_issue(client)
    api_error = APIError(503, {"message": "internal detail that should not leak"})
    with patch("backend.service.generate_issue_explanation", side_effect=api_error):
        response = client.post(f"/api/admin/issues/{created['public_id']}/explain")
    assert response.status_code == 200
    body = response.json()
    assert body["explanation"] is None
    assert body["error"] == "AI explanation is temporarily unavailable."
    assert "internal detail" not in response.text


def test_explain_malformed_gemini_output_degrades_gracefully(client):
    """A ValueError from the client (e.g. Gemini returned an empty
    response) must degrade the same way as EnvironmentError/APIError,
    never surfacing a raw exception."""
    created = _submit_issue(client)
    with patch(
        "backend.service.generate_issue_explanation",
        side_effect=ValueError("Gemini returned an empty response."),
    ):
        response = client.post(f"/api/admin/issues/{created['public_id']}/explain")
    assert response.status_code == 200
    body = response.json()
    assert body["explanation"] is None
    assert body["error"] == "AI explanation is temporarily unavailable."


def test_explain_does_not_fabricate_a_response_on_failure(client):
    """The failure path must never invent a plausible-looking explanation
    -- explanation must be exactly None, not an empty string or
    placeholder text."""
    created = _submit_issue(client)
    with patch("backend.service.generate_issue_explanation", side_effect=EnvironmentError("x")):
        response = client.post(f"/api/admin/issues/{created['public_id']}/explain")
    assert response.json()["explanation"] is None


# --- Official state is never mutated --------------------------------------------


def test_explain_never_mutates_official_issue_state(client):
    """Diffs the issue's full admin detail before/after requesting an
    explanation (success case) -- must be byte-for-byte identical."""
    created = _submit_issue(client)
    public_id = created["public_id"]

    before = client.get(f"/api/admin/issues/{public_id}").json()

    fake_output = IssueExplanationOutput(
        explanation="Looks correctly classified.", considerations=["Test consideration"]
    )
    with patch("backend.service.generate_issue_explanation", return_value=fake_output):
        client.post(f"/api/admin/issues/{public_id}/explain")

    after = client.get(f"/api/admin/issues/{public_id}").json()
    assert before == after


def test_explain_never_mutates_official_issue_state_on_failure(client):
    """Same diff, but for the Gemini-failure path -- degradation must
    also never touch official state."""
    created = _submit_issue(client)
    public_id = created["public_id"]

    before = client.get(f"/api/admin/issues/{public_id}").json()
    with patch("backend.service.generate_issue_explanation", side_effect=EnvironmentError("x")):
        client.post(f"/api/admin/issues/{public_id}/explain")
    after = client.get(f"/api/admin/issues/{public_id}").json()
    assert before == after


def test_explain_response_is_never_persisted_calling_twice_hits_gemini_twice(client):
    """No caching/persistence: two calls must invoke Gemini twice, proving
    nothing was stored from the first call."""
    created = _submit_issue(client)
    fake_output = IssueExplanationOutput(explanation="First call.", considerations=[])
    with patch(
        "backend.service.generate_issue_explanation", return_value=fake_output
    ) as mock_explain:
        client.post(f"/api/admin/issues/{created['public_id']}/explain")
        client.post(f"/api/admin/issues/{created['public_id']}/explain")
    assert mock_explain.call_count == 2


def test_explain_does_not_appear_in_admin_issue_detail_schema(client):
    """The explanation is never attached to the issue detail response --
    it only exists as this endpoint's own ephemeral return value."""
    created = _submit_issue(client)
    detail = client.get(f"/api/admin/issues/{created['public_id']}").json()
    assert "explanation" not in detail
    assert "considerations" not in detail


# --- 404 ------------------------------------------------------------------------


def test_explain_unknown_public_id_returns_404(client):
    response = client.post("/api/admin/issues/CIV-DOESNOTEXIST12/explain")
    assert response.status_code == 404
    assert "Traceback" not in response.text


def test_explain_unknown_public_id_never_calls_gemini(client):
    with patch("backend.service.generate_issue_explanation") as mock_explain:
        client.post("/api/admin/issues/CIV-DOESNOTEXIST12/explain")
    mock_explain.assert_not_called()
