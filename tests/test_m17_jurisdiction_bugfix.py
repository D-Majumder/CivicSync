"""
Milestone 17: fix the real architectural bug where SUBMITTED/unassigned
issues silently disappeared from jurisdiction-scoped authority views,
because jurisdiction was previously derived transitively via
Issue -> assigned_department -> Department -> Jurisdiction (and
assigned_department_id is intentionally NULL until an authority routes
an issue).

Issue.jurisdiction_id is now a direct, required, independent field. These
tests cover the 15 scenarios (A-O) from the M17 spec. Gemini is mocked
throughout -- no real Gemini calls. Every test uses an isolated, temporary
SQLite database via a get_db dependency override, never civicsync.db.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from ai.schemas import CivicIssue, IssueCategory, SeverityLevel
from backend.auth import get_current_authority
from backend.database import Base, build_engine, get_db
from backend.main import app
from backend.models import Department, Issue, IssueStatus, Jurisdiction, JurisdictionLevel
from backend.repository import get_default_jurisdiction_id


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
    """Two independent jurisdiction trees (Krishnanagar / Bengaluru),
    each with one department, mirroring tests/test_jurisdiction_scoping.py
    -- reused here so this file's cross-jurisdiction assertions are
    directly comparable."""
    db_path = tmp_path / "test_m17_jurisdiction_bugfix.db"
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


def _submit_issue(client, **overrides) -> dict:
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue(**overrides)):
        response = client.post("/api/issues", json={"text": "Some complaint."})
    assert response.status_code == 201
    return response.json()


# --- A & B. Submitted unassigned issue appears / doesn't disappear -------------


def test_a_submitted_unassigned_issue_appears_in_correct_jurisdiction(client):
    """Reproduces the exact reported production scenario: a fresh
    SUBMITTED issue with assigned_department_id=NULL must appear when
    scoped to its own jurisdiction."""
    created = _submit_issue(client)
    assert created["status"] == "SUBMITTED"

    response = client.get(
        "/api/admin/issues", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    )
    ids = {item["public_id"] for item in response.json()["items"]}
    assert created["public_id"] in ids


def test_b_unassigned_issue_does_not_disappear_because_department_is_null(client):
    """Direct proof the bug is fixed: an issue with NO department
    assignment at all must still be visible -- not silently excluded."""
    created = _submit_issue(client)

    detail = client.get(f"/api/admin/issues/{created['public_id']}").json()
    assert detail["assigned_department"] is None

    queue = client.get(
        "/api/admin/queue", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    ).json()
    ids = {item["public_id"] for item in queue["items"]}
    assert created["public_id"] in ids


# --- C. Cross-jurisdiction exclusion --------------------------------------------


def test_c_issue_from_jurisdiction_a_does_not_appear_when_scoped_to_b(client):
    created = _submit_issue(client)  # scoped to the default: Krishnanagar
    response = client.get("/api/admin/issues", params={"jurisdiction_code": "IN-KA-BLR-BBMP"})
    ids = {item["public_id"] for item in response.json()["items"]}
    assert created["public_id"] not in ids


# --- D. Assigned issue remains visible in its jurisdiction ----------------------


def test_d_assigned_issue_remains_visible_in_its_jurisdiction(client):
    created = _submit_issue(client)
    client.post(f"/api/issues/{created['public_id']}/status", json={"status": "CLASSIFIED"})
    client.post(
        f"/api/issues/{created['public_id']}/assignment",
        json={"department_code": "ROADS_TRANSPORT"},
    )
    response = client.get(
        "/api/admin/issues", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    )
    ids = {item["public_id"] for item in response.json()["items"]}
    assert created["public_id"] in ids


# --- E. Department filter still works independently ----------------------------


def test_e_department_filter_works_independently_of_jurisdiction(client):
    created = _submit_issue(client)
    # Unassigned -- department_code filter must correctly exclude it.
    response = client.get("/api/admin/issues", params={"department_code": "ROADS_TRANSPORT"})
    ids = {item["public_id"] for item in response.json()["items"]}
    assert created["public_id"] not in ids


# --- F. Combined jurisdiction + department filters ------------------------------


def test_f_combined_jurisdiction_and_department_filters(client):
    created = _submit_issue(client)
    client.post(f"/api/issues/{created['public_id']}/status", json={"status": "CLASSIFIED"})
    client.post(
        f"/api/issues/{created['public_id']}/assignment",
        json={"department_code": "ROADS_TRANSPORT"},
    )

    # Correct jurisdiction + correct department -> included.
    r1 = client.get(
        "/api/admin/issues",
        params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR", "department_code": "ROADS_TRANSPORT"},
    )
    assert created["public_id"] in {i["public_id"] for i in r1.json()["items"]}

    # Correct jurisdiction, WRONG department -> excluded.
    r2 = client.get(
        "/api/admin/issues",
        params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR", "department_code": "STREET_LIGHTING"},
    )
    assert created["public_id"] not in {i["public_id"] for i in r2.json()["items"]}


# --- G-K. Dashboard/funnel/severity/aging/recent-activity are jurisdiction-scoped -


def test_g_dashboard_summary_is_jurisdiction_scoped(client):
    created = _submit_issue(client)
    scoped = client.get(
        "/api/admin/dashboard/summary", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    ).json()
    other = client.get(
        "/api/admin/dashboard/summary", params={"jurisdiction_code": "IN-KA-BLR-BBMP"}
    ).json()
    assert scoped["total_issues"] == 1
    assert scoped["by_status"]["SUBMITTED"] == 1
    assert other["total_issues"] == 0


def test_h_funnel_is_jurisdiction_scoped(client):
    _submit_issue(client)
    scoped = client.get(
        "/api/admin/analytics/funnel", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    ).json()
    other = client.get(
        "/api/admin/analytics/funnel", params={"jurisdiction_code": "IN-KA-BLR-BBMP"}
    ).json()
    submitted_scoped = next(s["count"] for s in scoped["stages"] if s["status"] == "SUBMITTED")
    submitted_other = next(s["count"] for s in other["stages"] if s["status"] == "SUBMITTED")
    assert submitted_scoped == 1
    assert submitted_other == 0


def test_i_severity_distribution_is_jurisdiction_scoped(client):
    _submit_issue(client, severity=SeverityLevel.HIGH)
    scoped = client.get(
        "/api/admin/analytics/severity-distribution",
        params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"},
    ).json()
    other = client.get(
        "/api/admin/analytics/severity-distribution", params={"jurisdiction_code": "IN-KA-BLR-BBMP"}
    ).json()
    high_scoped = next(d["count"] for d in scoped["distribution"] if d["severity"] == "High")
    high_other = next(d["count"] for d in other["distribution"] if d["severity"] == "High")
    assert high_scoped == 1
    assert high_other == 0


def test_j_aging_is_jurisdiction_scoped(client):
    _submit_issue(client)
    scoped = client.get(
        "/api/admin/analytics/aging", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    ).json()
    other = client.get(
        "/api/admin/analytics/aging", params={"jurisdiction_code": "IN-KA-BLR-BBMP"}
    ).json()
    assert scoped["total_active_issues"] == 1
    assert other["total_active_issues"] == 0


def test_k_recent_activity_is_jurisdiction_scoped(client):
    created = _submit_issue(client)
    scoped = client.get(
        "/api/admin/analytics/recent-activity",
        params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"},
    ).json()
    other = client.get(
        "/api/admin/analytics/recent-activity", params={"jurisdiction_code": "IN-KA-BLR-BBMP"}
    ).json()
    scoped_ids = {a["public_id"] for a in scoped["activities"]}
    other_ids = {a["public_id"] for a in other["activities"]}
    assert created["public_id"] in scoped_ids
    assert created["public_id"] not in other_ids


# --- L. Civic Intelligence is jurisdiction-scoped --------------------------------


def test_l_civic_intelligence_is_jurisdiction_scoped(client):
    # 2 HIGH+ severity issues in Krishnanagar trigger the high-severity insight.
    _submit_issue(client, severity=SeverityLevel.CRITICAL)
    _submit_issue(client, severity=SeverityLevel.HIGH)

    scoped = client.get(
        "/api/admin/insights", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    ).json()
    other = client.get(
        "/api/admin/insights", params={"jurisdiction_code": "IN-KA-BLR-BBMP"}
    ).json()
    scoped_types = {i["insight_type"] for i in scoped["insights"]}
    other_types = {i["insight_type"] for i in other["insights"]}
    assert "high_severity_unresolved" in scoped_types
    assert "high_severity_unresolved" not in other_types


def test_l_unknown_jurisdiction_returns_404_for_insights(client):
    response = client.get("/api/admin/insights", params={"jurisdiction_code": "NOT-REAL"})
    assert response.status_code == 404


# --- M. Existing issue data survives migration -----------------------------------


def test_m_pre_existing_issue_without_jurisdiction_is_backfillable(client):
    """Simulates what the migration does: an Issue row that predates
    jurisdiction_id gets backfilled to a real jurisdiction, and its
    assigned_department_id (NULL, exactly like the reported production
    issue) is completely untouched by the backfill."""
    db_gen = app.dependency_overrides[get_db]()
    db = next(db_gen)
    try:
        krishnanagar = db.query(Jurisdiction).filter(
            Jurisdiction.code == "IN-WB-NADIA-KRISHNANAGAR"
        ).one()
        # Directly construct an Issue the way the migration's backfill
        # UPDATE would leave one -- jurisdiction_id set, everything else
        # (especially assigned_department_id) untouched.
        pre_existing = Issue(
            public_id="CIV-8AF152AA827A",
            original_text="Reported production issue.",
            category=IssueCategory.ROADS_AND_POTHOLES,
            problem="Pothole.",
            severity=SeverityLevel.HIGH,
            confidence=0.9,
            status=IssueStatus.SUBMITTED,
            jurisdiction_id=krishnanagar.id,
            # assigned_department_id intentionally omitted (stays NULL)
        )
        db.add(pre_existing)
        db.commit()
    finally:
        db.close()

    response = client.get(
        "/api/admin/issues", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    )
    ids = {item["public_id"] for item in response.json()["items"]}
    assert "CIV-8AF152AA827A" in ids


# --- N. New issue gets the configured default jurisdiction -----------------------


def test_n_new_issue_gets_configured_default_jurisdiction(client):
    created = _submit_issue(client)
    detail = client.get(f"/api/admin/issues/{created['public_id']}").json()
    # No jurisdiction field is exposed on the admin detail response
    # (matches the existing privacy/id-hiding convention), so verify via
    # the jurisdiction-scoped list instead -- the only way to observe it
    # through the public API surface.
    scoped = client.get(
        "/api/admin/issues", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    ).json()
    assert created["public_id"] in {i["public_id"] for i in scoped["items"]}


def test_n_default_jurisdiction_resolves_to_the_configured_code(client, monkeypatch):
    db_gen = app.dependency_overrides[get_db]()
    db = next(db_gen)
    try:
        jurisdiction_id = get_default_jurisdiction_id(db)
        expected = db.query(Jurisdiction).filter(
            Jurisdiction.code == "IN-WB-NADIA-KRISHNANAGAR"
        ).one()
        assert jurisdiction_id == expected.id
    finally:
        db.close()


# --- O. Missing/invalid configured default jurisdiction fails safely -------------


def test_o_missing_default_jurisdiction_env_var_fails_safely(client, monkeypatch):
    monkeypatch.delenv("CIVICSYNC_DEFAULT_JURISDICTION_CODE", raising=False)
    response = client.post("/api/issues", json={"text": "Some complaint."})
    assert response.status_code == 500
    # Sanitized -- never leaks the raw exception message or env var name.
    assert "CIVICSYNC_DEFAULT_JURISDICTION_CODE" not in response.text
    assert "Traceback" not in response.text


def test_o_invalid_default_jurisdiction_code_fails_safely(client, monkeypatch):
    monkeypatch.setenv("CIVICSYNC_DEFAULT_JURISDICTION_CODE", "NOT-A-REAL-JURISDICTION")
    response = client.post("/api/issues", json={"text": "Some complaint."})
    assert response.status_code == 500
    assert "NOT-A-REAL-JURISDICTION" not in response.text
    assert "Traceback" not in response.text


def test_o_missing_default_jurisdiction_never_persists_a_partial_issue(client, monkeypatch):
    """A failed submission (misconfigured jurisdiction) must not leave a
    partially-created Issue row behind."""
    monkeypatch.delenv("CIVICSYNC_DEFAULT_JURISDICTION_CODE", raising=False)
    client.post("/api/issues", json={"text": "Some complaint."})

    db_gen = app.dependency_overrides[get_db]()
    db = next(db_gen)
    try:
        assert db.query(Issue).count() == 0
    finally:
        db.close()


def test_o_inactive_default_jurisdiction_fails_safely(client, monkeypatch):
    """A jurisdiction that exists but is deactivated must be treated the
    same as a missing one -- never used silently."""
    db_gen = app.dependency_overrides[get_db]()
    db = next(db_gen)
    try:
        jurisdiction = db.query(Jurisdiction).filter(
            Jurisdiction.code == "IN-WB-NADIA-KRISHNANAGAR"
        ).one()
        jurisdiction.is_active = False
        db.commit()
    finally:
        db.close()

    response = client.post("/api/issues", json={"text": "Some complaint."})
    assert response.status_code == 500
