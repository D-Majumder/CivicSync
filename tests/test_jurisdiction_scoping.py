"""
Milestone 14, Priority 1: jurisdiction-operational filtering.

Proves that jurisdiction_code filtering on the admin issues/queue/stale
endpoints and the dashboard summary is genuinely operational -- not
cosmetic -- by constructing TWO independent, real jurisdiction trees
(each with its own department and issue) in an isolated test database and
verifying that scoping by one jurisdiction correctly INCLUDES its own
data and EXCLUDES the other's. This is test fixture data only; it is
never written to the real civicsync.db and is not presented anywhere as
production data.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from ai.schemas import CivicIssue, IssueCategory, SeverityLevel
from backend.database import Base, build_engine, get_db
from backend.main import app
from backend.models import Department, Issue, IssueStatus, Jurisdiction, JurisdictionLevel
from backend.repository import get_jurisdiction_subtree_department_ids


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
def two_jurisdiction_client(tmp_path):
    """Two independent, real jurisdiction trees under India, each with its
    own department and a real issue officially assigned to it:

        IN -> IN-WB (West Bengal) -> IN-WB-NADIA -> IN-WB-NADIA-KRISHNANAGAR
                                                        -> STREET_LIGHTING dept -> issue A (CIV test)
        IN -> IN-KA (Karnataka)   -> IN-KA-BLR   -> IN-KA-BLR-BBMP
                                                        -> ROADS dept -> issue B (CIV test)
    """
    db_path = tmp_path / "test_jurisdiction_scoping.db"
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

    dept_a = Department(code="STREET_LIGHTING", name="Street Lighting", jurisdiction_id=krishnanagar.id)
    dept_b = Department(code="ROADS_TRANSPORT", name="Roads and Transport", jurisdiction_id=bbmp.id)
    db.add_all([dept_a, dept_b])
    db.flush()

    stale_timestamp = (datetime.now(timezone.utc) - timedelta(hours=100)).replace(tzinfo=None)
    issue_a = Issue(
        public_id="CIV-TESTKRISHNANAGAR", original_text="Streetlight out in Krishnanagar.",
        category=IssueCategory.STREET_LIGHTING, problem="Streetlight not working.",
        severity=SeverityLevel.HIGH, confidence=0.9,
        status=IssueStatus.ROUTED, assigned_department_id=dept_a.id,
        updated_at=stale_timestamp,
    )
    issue_b = Issue(
        public_id="CIV-TESTBENGALURU", original_text="Pothole in Bengaluru.",
        category=IssueCategory.ROADS_AND_POTHOLES, problem="Large pothole.",
        severity=SeverityLevel.CRITICAL, confidence=0.85,
        status=IssueStatus.ROUTED, assigned_department_id=dept_b.id,
        updated_at=stale_timestamp,
    )
    db.add_all([issue_a, issue_b])
    db.commit()
    db.close()

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


# --- Repository-level: subtree resolution across two independent trees -------


def test_subtree_resolution_is_isolated_between_independent_trees(two_jurisdiction_client, tmp_path):
    """Direct repository check: resolving Karnataka's subtree must not
    include West Bengal's department, and vice versa."""
    from backend.database import get_db as real_get_db

    db_gen = app.dependency_overrides[real_get_db]()
    db = next(db_gen)
    try:
        wb_dept_ids = get_jurisdiction_subtree_department_ids(db, "IN-WB")
        ka_dept_ids = get_jurisdiction_subtree_department_ids(db, "IN-KA")
        wb_codes = {d.code for d in db.query(Department).filter(Department.id.in_(wb_dept_ids)).all()}
        ka_codes = {d.code for d in db.query(Department).filter(Department.id.in_(ka_dept_ids)).all()}
        assert wb_codes == {"STREET_LIGHTING"}
        assert ka_codes == {"ROADS_TRANSPORT"}
        assert wb_codes.isdisjoint(ka_codes)
    finally:
        db.close()


# --- GET /api/admin/issues ----------------------------------------------------


def test_admin_issues_scoped_to_west_bengal_excludes_karnataka(two_jurisdiction_client):
    response = two_jurisdiction_client.get("/api/admin/issues", params={"jurisdiction_code": "IN-WB"})
    assert response.status_code == 200
    ids = {item["public_id"] for item in response.json()["items"]}
    assert ids == {"CIV-TESTKRISHNANAGAR"}


def test_admin_issues_scoped_to_karnataka_excludes_west_bengal(two_jurisdiction_client):
    response = two_jurisdiction_client.get("/api/admin/issues", params={"jurisdiction_code": "IN-KA"})
    assert response.status_code == 200
    ids = {item["public_id"] for item in response.json()["items"]}
    assert ids == {"CIV-TESTBENGALURU"}


def test_admin_issues_scoped_to_country_includes_both(two_jurisdiction_client):
    response = two_jurisdiction_client.get("/api/admin/issues", params={"jurisdiction_code": "IN"})
    ids = {item["public_id"] for item in response.json()["items"]}
    assert ids == {"CIV-TESTKRISHNANAGAR", "CIV-TESTBENGALURU"}


def test_admin_issues_district_level_scoping_reaches_local_body_descendant(two_jurisdiction_client):
    """A DISTRICT-level filter must include issues under the LOCAL_BODY
    beneath it -- proves the subtree walk, not just exact-match."""
    response = two_jurisdiction_client.get(
        "/api/admin/issues", params={"jurisdiction_code": "IN-WB-NADIA"}
    )
    ids = {item["public_id"] for item in response.json()["items"]}
    assert ids == {"CIV-TESTKRISHNANAGAR"}


def test_admin_issues_unknown_jurisdiction_code_returns_404(two_jurisdiction_client):
    response = two_jurisdiction_client.get(
        "/api/admin/issues", params={"jurisdiction_code": "NOT-REAL"}
    )
    assert response.status_code == 404


def test_admin_issues_jurisdiction_and_severity_filters_compose(two_jurisdiction_client):
    """jurisdiction_code must AND with other existing filters, not
    replace them."""
    response = two_jurisdiction_client.get(
        "/api/admin/issues",
        params={"jurisdiction_code": "IN-WB", "severity": "Critical"},
    )
    # Krishnanagar's issue is HIGH, not CRITICAL -- combined filter excludes it.
    assert response.json()["items"] == []


# --- GET /api/admin/queue ------------------------------------------------------


def test_queue_scoped_to_jurisdiction(two_jurisdiction_client):
    response = two_jurisdiction_client.get("/api/admin/queue", params={"jurisdiction_code": "IN-KA"})
    assert response.status_code == 200
    ids = {item["public_id"] for item in response.json()["items"]}
    assert ids == {"CIV-TESTBENGALURU"}


def test_queue_unknown_jurisdiction_returns_404(two_jurisdiction_client):
    response = two_jurisdiction_client.get(
        "/api/admin/queue", params={"jurisdiction_code": "NOT-REAL"}
    )
    assert response.status_code == 404


# --- GET /api/admin/issues/stale ------------------------------------------------


def test_stale_scoped_to_jurisdiction_with_default_threshold(two_jurisdiction_client):
    """Fixture issues are backdated 100h; the default 48h threshold makes
    both genuinely stale -- isolates the jurisdiction_code filter as the
    thing under test."""
    response = two_jurisdiction_client.get(
        "/api/admin/issues/stale",
        params={"jurisdiction_code": "IN-WB"},
    )
    assert response.status_code == 200
    ids = {item["public_id"] for item in response.json()["items"]}
    assert ids == {"CIV-TESTKRISHNANAGAR"}


def test_stale_unknown_jurisdiction_returns_404(two_jurisdiction_client):
    response = two_jurisdiction_client.get(
        "/api/admin/issues/stale", params={"jurisdiction_code": "NOT-REAL"}
    )
    assert response.status_code == 404


# --- GET /api/admin/dashboard/summary -------------------------------------------


def test_dashboard_summary_scoped_to_west_bengal(two_jurisdiction_client):
    response = two_jurisdiction_client.get(
        "/api/admin/dashboard/summary", params={"jurisdiction_code": "IN-WB"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_issues"] == 1
    assert body["by_severity"]["High"] == 1
    assert body["by_severity"]["Critical"] == 0  # Bengaluru's issue correctly excluded


def test_dashboard_summary_unscoped_includes_both(two_jurisdiction_client):
    response = two_jurisdiction_client.get("/api/admin/dashboard/summary")
    assert response.json()["total_issues"] == 2


def test_dashboard_summary_by_department_always_shows_all_active_departments(
    two_jurisdiction_client,
):
    """Documented, intentional behavior: by_department is never scoped
    (each department's own count already reads correctly either way)."""
    response = two_jurisdiction_client.get(
        "/api/admin/dashboard/summary", params={"jurisdiction_code": "IN-WB"}
    )
    dept_codes = {d["code"] for d in response.json()["by_department"]}
    assert dept_codes == {"STREET_LIGHTING", "ROADS_TRANSPORT"}


def test_dashboard_summary_unknown_jurisdiction_returns_404(two_jurisdiction_client):
    response = two_jurisdiction_client.get(
        "/api/admin/dashboard/summary", params={"jurisdiction_code": "NOT-REAL"}
    )
    assert response.status_code == 404


# --- Regression: existing unscoped behavior is unchanged ------------------------


def test_admin_issues_without_jurisdiction_code_is_unaffected(two_jurisdiction_client):
    """Omitting jurisdiction_code entirely must behave exactly as before
    M14 -- no scoping applied, all issues returned."""
    response = two_jurisdiction_client.get("/api/admin/issues")
    ids = {item["public_id"] for item in response.json()["items"]}
    assert ids == {"CIV-TESTKRISHNANAGAR", "CIV-TESTBENGALURU"}


def test_queue_without_jurisdiction_code_is_unaffected(two_jurisdiction_client):
    response = two_jurisdiction_client.get("/api/admin/queue")
    ids = {item["public_id"] for item in response.json()["items"]}
    assert ids == {"CIV-TESTKRISHNANAGAR", "CIV-TESTBENGALURU"}
