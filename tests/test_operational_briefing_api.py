"""
Milestone 15: AI Operational Briefing
(POST /api/admin/analytics/operational-briefing).

Gemini is mocked in every test in this file, matching the established
pattern in tests/test_issue_explanation_api.py and
tests/test_insights_api.py -- no real Gemini calls. Every test uses an
isolated, temporary SQLite database via a get_db dependency override --
never civicsync.db.

Reuses the two-independent-jurisdictions fixture pattern from
tests/test_jurisdiction_scoping.py (Milestone 14) to prove jurisdiction
scoping is genuinely respected here too, not merely displayed.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from google.genai.errors import APIError
from sqlalchemy.orm import sessionmaker

from ai.schemas import CivicIssue, IssueCategory, OperationalBriefingOutput, SeverityLevel
from backend.database import Base, build_engine, get_db
from backend.auth import get_current_authority
from backend.main import app
from backend.models import Department, Issue, IssueStatus, Jurisdiction, JurisdictionLevel


def _sample_civic_issue(**overrides) -> CivicIssue:
    base = dict(
        original_text="Sample complaint text.",
        category=IssueCategory.OTHER,
        problem="Sample problem.",
        severity=SeverityLevel.MEDIUM,
        confidence=0.75,
    )
    base.update(overrides)
    return CivicIssue(**base)


@pytest.fixture
def briefing_client(tmp_path):
    """Two independent, real jurisdiction trees under India, each with
    its own department and active issues -- mirrors
    tests/test_jurisdiction_scoping.py's fixture exactly, so this file's
    exclusion tests are directly comparable to M14's.

        IN -> IN-WB (West Bengal) -> IN-WB-NADIA -> IN-WB-NADIA-KRISHNANAGAR
                                                        -> STREET_LIGHTING dept -> 2 active issues
                                                        -> WATER_SANITATION dept -> 1 active issue
        IN -> IN-KA (Karnataka)   -> IN-KA-BLR   -> IN-KA-BLR-BBMP
                                                        -> ROADS_TRANSPORT dept -> 1 active issue (unrelated)
    """
    db_path = tmp_path / "test_operational_briefing.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    db = TestingSessionLocal()

    country = Jurisdiction(code="IN", name="India", level=JurisdictionLevel.COUNTRY, country_code="IN")
    db.add(country)
    db.flush()
    wb = Jurisdiction(
        code="IN-WB", name="West Bengal", level=JurisdictionLevel.STATE,
        country_code="IN", parent_jurisdiction_id=country.id,
    )
    db.add(wb)
    db.flush()
    nadia = Jurisdiction(
        code="IN-WB-NADIA", name="Nadia", level=JurisdictionLevel.DISTRICT,
        country_code="IN", parent_jurisdiction_id=wb.id,
    )
    db.add(nadia)
    db.flush()
    krishnanagar = Jurisdiction(
        code="IN-WB-NADIA-KRISHNANAGAR", name="Krishnanagar Municipality",
        level=JurisdictionLevel.LOCAL_BODY, country_code="IN", parent_jurisdiction_id=nadia.id,
    )
    db.add(krishnanagar)
    db.flush()

    ka = Jurisdiction(
        code="IN-KA", name="Karnataka", level=JurisdictionLevel.STATE,
        country_code="IN", parent_jurisdiction_id=country.id,
    )
    db.add(ka)
    db.flush()
    blr = Jurisdiction(
        code="IN-KA-BLR", name="Bengaluru Urban", level=JurisdictionLevel.DISTRICT,
        country_code="IN", parent_jurisdiction_id=ka.id,
    )
    db.add(blr)
    db.flush()
    bbmp = Jurisdiction(
        code="IN-KA-BLR-BBMP", name="Bruhat Bengaluru Mahanagara Palike",
        level=JurisdictionLevel.LOCAL_BODY, country_code="IN", parent_jurisdiction_id=blr.id,
    )
    db.add(bbmp)
    db.flush()

    dept_lighting = Department(code="STREET_LIGHTING", name="Street Lighting", jurisdiction_id=krishnanagar.id)
    dept_water = Department(code="WATER_SANITATION", name="Water and Sanitation", jurisdiction_id=krishnanagar.id)
    dept_roads = Department(code="ROADS_TRANSPORT", name="Roads and Transport", jurisdiction_id=bbmp.id)
    db.add_all([dept_lighting, dept_water, dept_roads])
    db.flush()

    issue_1 = Issue(
        public_id="CIV-TESTLIGHT01", original_text="Streetlight out near school.",
        category=IssueCategory.STREET_LIGHTING, problem="Streetlight not working.",
        severity=SeverityLevel.HIGH, confidence=0.9,
        status=IssueStatus.ROUTED, assigned_department_id=dept_lighting.id,
    )
    issue_2 = Issue(
        public_id="CIV-TESTLIGHT02", original_text="Another streetlight out.",
        category=IssueCategory.STREET_LIGHTING, problem="Streetlight not working.",
        severity=SeverityLevel.MEDIUM, confidence=0.8,
        status=IssueStatus.ACKNOWLEDGED, assigned_department_id=dept_lighting.id,
    )
    issue_3 = Issue(
        public_id="CIV-TESTWATER01", original_text="Water main leaking.",
        category=IssueCategory.WATER_SUPPLY, problem="Water main leak.",
        severity=SeverityLevel.CRITICAL, confidence=0.95,
        status=IssueStatus.IN_PROGRESS, assigned_department_id=dept_water.id,
    )
    # A CLOSED issue in the same scope -- must be excluded (active-only).
    issue_closed = Issue(
        public_id="CIV-TESTCLOSED01", original_text="Old resolved issue.",
        category=IssueCategory.STREET_LIGHTING, problem="Old issue.",
        severity=SeverityLevel.LOW, confidence=0.7,
        status=IssueStatus.CLOSED, assigned_department_id=dept_lighting.id,
    )
    issue_bengaluru = Issue(
        public_id="CIV-TESTBLR01", original_text="Pothole in Bengaluru.",
        category=IssueCategory.ROADS_AND_POTHOLES, problem="Large pothole.",
        severity=SeverityLevel.CRITICAL, confidence=0.85,
        status=IssueStatus.ROUTED, assigned_department_id=dept_roads.id,
    )
    db.add_all([issue_1, issue_2, issue_3, issue_closed, issue_bengaluru])
    db.commit()
    db.close()

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


def _fake_briefing(**overrides) -> OperationalBriefingOutput:
    base = dict(
        briefing="Krishnanagar Municipality has 3 active issues, concentrated in Street Lighting.",
        key_observations=["2 of 3 active issues are Street Lighting", "1 Critical-severity water issue"],
        priority_signals=["The Critical water main leak warrants immediate attention"],
        considerations=["Consider whether the two lighting issues are related"],
    )
    base.update(overrides)
    return OperationalBriefingOutput(**base)


# --- 1-2. Gemini success + correct structured payload -------------------------


def test_briefing_gemini_success_path(briefing_client):
    fake_output = _fake_briefing()
    with patch("backend.service.generate_operational_briefing", return_value=fake_output):
        response = briefing_client.post(
            "/api/admin/analytics/operational-briefing",
            params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"},
        )
    assert response.status_code == 200
    body = response.json()
    assert "Krishnanagar" in body["briefing"]
    assert len(body["key_observations"]) == 2
    assert len(body["priority_signals"]) == 1
    assert body["error"] is None


def test_briefing_payload_contains_correct_structured_fields(briefing_client):
    captured = {}

    def _capture(payload):
        captured.update(payload)
        return _fake_briefing()

    with patch("backend.service.generate_operational_briefing", side_effect=_capture):
        response = briefing_client.post(
            "/api/admin/analytics/operational-briefing",
            params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"},
        )
    assert response.status_code == 200
    assert captured["jurisdiction_code"] == "IN-WB-NADIA-KRISHNANAGAR"
    assert captured["department_code"] is None
    assert captured["total_active_issues"] == 3  # excludes the CLOSED issue
    categories = {i["category"] for i in captured["issues"]}
    assert categories == {"Street Lighting", "Water Supply"}
    assert all(set(i.keys()) == {"category", "severity", "status", "department"} for i in captured["issues"])


# --- 3-4. Privacy: never send original_text or public_id ----------------------


def test_briefing_never_sends_original_text(briefing_client):
    captured = {}

    def _capture(payload):
        captured.update(payload)
        return _fake_briefing()

    with patch("backend.service.generate_operational_briefing", side_effect=_capture):
        briefing_client.post(
            "/api/admin/analytics/operational-briefing",
            params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"},
        )
    payload_str = str(captured)
    assert "original_text" not in payload_str
    assert "Streetlight out near school" not in payload_str
    assert "Water main leaking" not in payload_str


def test_briefing_never_sends_public_id(briefing_client):
    captured = {}

    def _capture(payload):
        captured.update(payload)
        return _fake_briefing()

    with patch("backend.service.generate_operational_briefing", side_effect=_capture):
        briefing_client.post(
            "/api/admin/analytics/operational-briefing",
            params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"},
        )
    payload_str = str(captured)
    assert "CIV-TESTLIGHT01" not in payload_str
    assert "public_id" not in payload_str


# --- 5. Unrelated jurisdiction data excluded -----------------------------------


def test_briefing_excludes_unrelated_jurisdiction(briefing_client):
    captured = {}

    def _capture(payload):
        captured.update(payload)
        return _fake_briefing()

    with patch("backend.service.generate_operational_briefing", side_effect=_capture):
        briefing_client.post(
            "/api/admin/analytics/operational-briefing",
            params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"},
        )
    # Bengaluru's issue (Roads and Potholes) must never appear.
    categories = {i["category"] for i in captured["issues"]}
    assert "Roads and Potholes" not in categories
    assert captured["total_active_issues"] == 3


def test_briefing_karnataka_scope_excludes_west_bengal(briefing_client):
    captured = {}

    def _capture(payload):
        captured.update(payload)
        return _fake_briefing()

    with patch("backend.service.generate_operational_briefing", side_effect=_capture):
        briefing_client.post(
            "/api/admin/analytics/operational-briefing",
            params={"jurisdiction_code": "IN-KA"},
        )
    categories = {i["category"] for i in captured["issues"]}
    assert categories == {"Roads and Potholes"}
    assert captured["total_active_issues"] == 1


# --- 6. Department filtering ----------------------------------------------------


def test_briefing_department_filter_narrows_scope(briefing_client):
    captured = {}

    def _capture(payload):
        captured.update(payload)
        return _fake_briefing()

    with patch("backend.service.generate_operational_briefing", side_effect=_capture):
        response = briefing_client.post(
            "/api/admin/analytics/operational-briefing",
            params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR", "department_code": "WATER_SANITATION"},
        )
    assert response.status_code == 200
    assert captured["department_code"] == "WATER_SANITATION"
    assert captured["total_active_issues"] == 1
    assert captured["issues"][0]["category"] == "Water Supply"


# --- 7. Unknown jurisdiction returns 404 ----------------------------------------


def test_briefing_unknown_jurisdiction_returns_404(briefing_client):
    response = briefing_client.post(
        "/api/admin/analytics/operational-briefing", params={"jurisdiction_code": "NOT-REAL"}
    )
    assert response.status_code == 404
    assert "Traceback" not in response.text


def test_briefing_unknown_jurisdiction_never_calls_gemini(briefing_client):
    with patch("backend.service.generate_operational_briefing") as mock_gen:
        briefing_client.post(
            "/api/admin/analytics/operational-briefing", params={"jurisdiction_code": "NOT-REAL"}
        )
    mock_gen.assert_not_called()


# --- 8-10. Gemini failure modes degrade gracefully ------------------------------


def test_briefing_gemini_environment_error_degrades_gracefully(briefing_client):
    with patch(
        "backend.service.generate_operational_briefing",
        side_effect=EnvironmentError("GEMINI_API_KEY is not set. secret-value"),
    ):
        response = briefing_client.post(
            "/api/admin/analytics/operational-briefing",
            params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["briefing"] is None
    assert body["error"] == "AI operational briefing is temporarily unavailable."
    assert "secret-value" not in response.text
    assert "GEMINI_API_KEY" not in response.text


def test_briefing_gemini_api_error_degrades_gracefully(briefing_client):
    api_error = APIError(503, {"message": "internal detail that should not leak"})
    with patch("backend.service.generate_operational_briefing", side_effect=api_error):
        response = briefing_client.post(
            "/api/admin/analytics/operational-briefing",
            params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["briefing"] is None
    assert body["error"] == "AI operational briefing is temporarily unavailable."
    assert "internal detail" not in response.text


def test_briefing_gemini_value_error_degrades_gracefully(briefing_client):
    with patch(
        "backend.service.generate_operational_briefing",
        side_effect=ValueError("Gemini returned an empty response."),
    ):
        response = briefing_client.post(
            "/api/admin/analytics/operational-briefing",
            params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["briefing"] is None
    assert body["error"] == "AI operational briefing is temporarily unavailable."


# --- 11-12. Never persisted; repeated calls invoke Gemini again ----------------


def test_briefing_response_never_appears_in_dashboard_summary(briefing_client):
    fake_output = _fake_briefing()
    with patch("backend.service.generate_operational_briefing", return_value=fake_output):
        briefing_client.post(
            "/api/admin/analytics/operational-briefing",
            params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"},
        )
    summary = briefing_client.get(
        "/api/admin/dashboard/summary", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    ).json()
    assert "briefing" not in str(summary)


def test_briefing_repeated_calls_invoke_gemini_again(briefing_client):
    fake_output = _fake_briefing()
    with patch(
        "backend.service.generate_operational_briefing", return_value=fake_output
    ) as mock_gen:
        briefing_client.post(
            "/api/admin/analytics/operational-briefing",
            params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"},
        )
        briefing_client.post(
            "/api/admin/analytics/operational-briefing",
            params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"},
        )
    assert mock_gen.call_count == 2


# --- 13-14. Official issue state is byte-identical before/after ----------------


def test_briefing_never_mutates_official_issue_state_on_success(briefing_client):
    before = briefing_client.get("/api/admin/issues/CIV-TESTLIGHT01").json()
    fake_output = _fake_briefing()
    with patch("backend.service.generate_operational_briefing", return_value=fake_output):
        briefing_client.post(
            "/api/admin/analytics/operational-briefing",
            params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"},
        )
    after = briefing_client.get("/api/admin/issues/CIV-TESTLIGHT01").json()
    assert before == after


def test_briefing_never_mutates_official_issue_state_on_failure(briefing_client):
    before = briefing_client.get("/api/admin/issues/CIV-TESTWATER01").json()
    with patch("backend.service.generate_operational_briefing", side_effect=EnvironmentError("x")):
        briefing_client.post(
            "/api/admin/analytics/operational-briefing",
            params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"},
        )
    after = briefing_client.get("/api/admin/issues/CIV-TESTWATER01").json()
    assert before == after


# --- 15. Insufficient data does not produce a fabricated pattern ---------------


def test_briefing_empty_scope_does_not_call_gemini_and_is_honest(briefing_client):
    """A department with zero active issues in this jurisdiction must not
    trigger a Gemini call or a fabricated briefing -- a plain, honest
    message is returned instead."""
    with patch("backend.service.generate_operational_briefing") as mock_gen:
        response = briefing_client.post(
            "/api/admin/analytics/operational-briefing",
            params={"jurisdiction_code": "IN-KA-BLR-BBMP", "department_code": "STREET_LIGHTING"},
        )
    mock_gen.assert_not_called()
    assert response.status_code == 200
    body = response.json()
    assert body["briefing"] is None
    assert body["key_observations"] == []
    assert "No active issues" in body["error"]


def test_briefing_prompt_instructs_against_single_issue_pattern_claims(briefing_client):
    """Static check that the system prompt actually contains the
    single-issue-is-not-a-pattern instruction required by the spec."""
    from ai.prompts import OPERATIONAL_BRIEFING_SYSTEM_PROMPT

    assert "single issue" in OPERATIONAL_BRIEFING_SYSTEM_PROMPT.lower()
    assert "pattern" in OPERATIONAL_BRIEFING_SYSTEM_PROMPT.lower()


# --- 16. Existing M14 endpoints continue working --------------------------------


def test_existing_explain_endpoint_still_works(briefing_client):
    from ai.schemas import IssueExplanationOutput

    fake_explanation = IssueExplanationOutput(explanation="Still works.", considerations=[])
    with patch("backend.service.generate_issue_explanation", return_value=fake_explanation):
        response = briefing_client.post("/api/admin/issues/CIV-TESTLIGHT01/explain")
    assert response.status_code == 200
    assert response.json()["explanation"] == "Still works."


def test_existing_jurisdiction_list_endpoint_still_works(briefing_client):
    response = briefing_client.get("/api/jurisdictions", params={"level": "LOCAL_BODY"})
    assert response.status_code == 200
    codes = {j["code"] for j in response.json()["jurisdictions"]}
    assert codes == {"IN-WB-NADIA-KRISHNANAGAR", "IN-KA-BLR-BBMP"}


# --- 17. Regression: M14 jurisdiction scoping on other endpoints ---------------


def test_admin_issues_jurisdiction_scoping_still_works(briefing_client):
    response = briefing_client.get(
        "/api/admin/issues", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    )
    ids = {item["public_id"] for item in response.json()["items"]}
    assert "CIV-TESTBLR01" not in ids
    assert ids == {"CIV-TESTLIGHT01", "CIV-TESTLIGHT02", "CIV-TESTWATER01", "CIV-TESTCLOSED01"}


def test_operational_queue_jurisdiction_scoping_still_works(briefing_client):
    response = briefing_client.get(
        "/api/admin/queue", params={"jurisdiction_code": "IN-KA"}
    )
    ids = {item["public_id"] for item in response.json()["items"]}
    assert ids == {"CIV-TESTBLR01"}
