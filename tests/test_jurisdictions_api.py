"""
API-level tests for CivicSync's jurisdiction hierarchy (Milestone 13):
GET /api/jurisdictions, GET /api/jurisdictions/{code}, plus regression
checks that existing issue/department/tracking endpoints are unaffected.

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
from backend.models import Department, Jurisdiction, JurisdictionLevel


def _sample_civic_issue(**overrides) -> CivicIssue:
    base = dict(
        original_text="There has been no street light near our school for two weeks.",
        category=IssueCategory.STREET_LIGHTING,
        problem="Lack of functioning street light near a school.",
        severity=SeverityLevel.MEDIUM,
        confidence=0.82,
    )
    base.update(overrides)
    return CivicIssue(**base)


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "test_jurisdictions_api.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    seed_db = TestingSessionLocal()
    country = Jurisdiction(code="IN", name="India", level=JurisdictionLevel.COUNTRY, country_code="IN")
    seed_db.add(country)
    seed_db.flush()
    state = Jurisdiction(
        code="IN-WB", name="West Bengal", level=JurisdictionLevel.STATE,
        country_code="IN", parent_jurisdiction_id=country.id,
    )
    seed_db.add(state)
    seed_db.flush()
    district = Jurisdiction(
        code="IN-WB-NADIA", name="Nadia", level=JurisdictionLevel.DISTRICT,
        country_code="IN", parent_jurisdiction_id=state.id,
    )
    seed_db.add(district)
    seed_db.flush()
    local_body = Jurisdiction(
        code="IN-WB-NADIA-KRISHNANAGAR", name="Krishnanagar Municipality",
        level=JurisdictionLevel.LOCAL_BODY, country_code="IN",
        parent_jurisdiction_id=district.id,
    )
    seed_db.add(local_body)
    seed_db.flush()
    seed_db.add(
        Department(
            code="STREET_LIGHTING", name="Street Lighting", is_active=True,
            jurisdiction_id=local_body.id,
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


def _submit_issue(client, **overrides) -> dict:
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue(**overrides)):
        response = client.post("/api/issues", json={"text": "Some complaint."})
    assert response.status_code == 201
    return response.json()


# --- GET /api/jurisdictions ---------------------------------------------------


def test_list_jurisdictions_returns_all_four_levels(client):
    response = client.get("/api/jurisdictions")
    assert response.status_code == 200
    codes = {j["code"] for j in response.json()["jurisdictions"]}
    assert codes == {"IN", "IN-WB", "IN-WB-NADIA", "IN-WB-NADIA-KRISHNANAGAR"}


def test_list_jurisdictions_filters_by_level(client):
    response = client.get("/api/jurisdictions", params={"level": "LOCAL_BODY"})
    body = response.json()["jurisdictions"]
    assert len(body) == 1
    assert body[0]["code"] == "IN-WB-NADIA-KRISHNANAGAR"


def test_list_jurisdictions_filters_by_country_code(client):
    response = client.get("/api/jurisdictions", params={"country_code": "IN"})
    assert len(response.json()["jurisdictions"]) == 4

    response = client.get("/api/jurisdictions", params={"country_code": "BR"})
    assert response.json()["jurisdictions"] == []


def test_list_jurisdictions_each_entry_includes_ancestry(client):
    response = client.get("/api/jurisdictions", params={"level": "LOCAL_BODY"})
    entry = response.json()["jurisdictions"][0]
    ancestry_codes = [a["code"] for a in entry["ancestry"]]
    assert ancestry_codes == ["IN", "IN-WB", "IN-WB-NADIA", "IN-WB-NADIA-KRISHNANAGAR"]


def test_list_jurisdictions_excludes_internal_ids(client):
    response = client.get("/api/jurisdictions")
    raw_text = response.text
    assert '"id"' not in raw_text
    assert "parent_jurisdiction_id" not in raw_text


def test_list_jurisdictions_does_not_call_gemini(client):
    with (
        patch("backend.main.analyze_complaint") as mock_main,
        patch("backend.service.analyze_complaint") as mock_service,
    ):
        response = client.get("/api/jurisdictions")
    assert response.status_code == 200
    mock_main.assert_not_called()
    mock_service.assert_not_called()


# --- GET /api/jurisdictions/{code} --------------------------------------------


def test_get_jurisdiction_by_code(client):
    response = client.get("/api/jurisdictions/IN-WB-NADIA")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Nadia"
    assert body["level"] == "DISTRICT"
    assert body["parent_code"] == "IN-WB"
    assert [a["code"] for a in body["ancestry"]] == ["IN", "IN-WB", "IN-WB-NADIA"]


def test_get_jurisdiction_country_has_null_parent_code(client):
    response = client.get("/api/jurisdictions/IN")
    assert response.json()["parent_code"] is None


def test_get_jurisdiction_unknown_code_returns_404(client):
    response = client.get("/api/jurisdictions/NOT-A-REAL-CODE")
    assert response.status_code == 404
    assert "Traceback" not in response.text


# --- Jurisdiction scoping via department -------------------------------------


def test_department_linked_to_jurisdiction_is_visible_via_admin_department_summary(client):
    """The one existing integration point (Department.jurisdiction_id)
    doesn't break the pre-existing department summary endpoint."""
    response = client.get("/api/admin/departments/STREET_LIGHTING/summary")
    assert response.status_code == 200
    assert response.json()["code"] == "STREET_LIGHTING"


# --- Regression: existing endpoints unaffected by M13 -------------------------


def test_existing_post_issues_still_works(client):
    created = _submit_issue(client)
    assert created["status"] == "SUBMITTED"


def test_existing_get_departments_still_works_and_is_unaffected(client):
    """GET /api/departments (the pre-existing, unrelated endpoint) must
    not have started requiring or exposing jurisdiction fields -- M13
    deliberately did not touch DepartmentResponse."""
    response = client.get("/api/departments")
    assert response.status_code == 200
    body = response.json()[0]
    assert set(body.keys()) == {"code", "name", "description", "is_active"}
    assert "jurisdiction" not in body
    assert "jurisdiction_id" not in body


def test_existing_public_tracking_still_works_and_has_no_jurisdiction_leak(client):
    created = _submit_issue(client)
    response = client.get(f"/api/track/{created['public_id']}")
    assert response.status_code == 200
    assert "jurisdiction" not in response.text.lower()


def test_health_still_works(client):
    assert client.get("/health").status_code == 200
