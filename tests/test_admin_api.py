"""
API-level tests for CivicSync's internal authority operations API
(Milestone 8): GET /api/admin/issues, GET /api/admin/issues/{public_id},
GET /api/admin/issues/stale, GET /api/admin/dashboard/summary,
GET /api/admin/departments/{department_code}/summary, GET /api/admin/queue.

analyze_complaint() is mocked (no Gemini calls anywhere in this file).
Every test uses the `client` fixture, which wires the FastAPI app to an
isolated, temporary SQLite database via a get_db dependency override --
never civicsync.db.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
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
        location="near our school",
        severity=SeverityLevel.MEDIUM,
        confidence=0.82,
        suggested_department="Electrical / Street Lighting Department",
    )
    base.update(overrides)
    return CivicIssue(**base)


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "test_admin_api.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    seed_db = TestingSessionLocal()
    jurisdiction = Jurisdiction(
        code="IN-WB-NADIA-KRISHNANAGAR", name="Krishnanagar Municipality",
        level=JurisdictionLevel.LOCAL_BODY, country_code="IN",
    )
    seed_db.add(jurisdiction)
    seed_db.commit()
    seed_db.add_all(
        [
            Department(code="STREET_LIGHTING", name="Street Lighting", is_active=True),
            Department(code="ELECTRICITY", name="Electricity", is_active=True),
            Department(code="ROADS_TRANSPORT", name="Roads & Transport", is_active=True),
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
    test_client.engine = engine
    test_client.SessionLocal = TestingSessionLocal
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


def _advance(client, public_id: str, *targets: str) -> dict:
    body = None
    for target in targets:
        response = client.post(f"/api/issues/{public_id}/status", json={"status": target})
        assert response.status_code == 200, response.json()
        body = response.json()
    return body


def _assign(client, public_id: str, department_code: str) -> dict:
    response = client.post(
        f"/api/issues/{public_id}/assignment", json={"department_code": department_code}
    )
    assert response.status_code == 200, response.json()
    return response.json()


def _backdate_updated_at(client, public_id: str, hours_ago: float) -> None:
    """Directly UPDATE updated_at via raw SQL, bypassing the ORM's
    onupdate hook, to simulate an issue that hasn't changed recently."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).replace(tzinfo=None)
    db = client.SessionLocal()
    try:
        db.execute(
            text("UPDATE issues SET updated_at = :ts WHERE public_id = :pid"),
            {"ts": cutoff.isoformat(sep=" "), "pid": public_id},
        )
        db.commit()
    finally:
        db.close()


# ============================================================================
# AUTHORITY ISSUE LIST -- GET /api/admin/issues
# ============================================================================


def test_returns_paginated_issues(client):
    for _ in range(3):
        _submit_issue(client)
    response = client.get("/api/admin/issues")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3


def test_total_is_correct_before_pagination(client):
    for _ in range(5):
        _submit_issue(client)
    response = client.get("/api/admin/issues", params={"limit": 2, "offset": 0})
    body = response.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2


def test_limit_works(client):
    for _ in range(5):
        _submit_issue(client)
    response = client.get("/api/admin/issues", params={"limit": 3})
    assert len(response.json()["items"]) == 3


def test_offset_works(client):
    for _ in range(5):
        _submit_issue(client)
    page1 = client.get("/api/admin/issues", params={"limit": 2, "offset": 0}).json()
    page2 = client.get("/api/admin/issues", params={"limit": 2, "offset": 2}).json()
    ids_page1 = {i["public_id"] for i in page1["items"]}
    ids_page2 = {i["public_id"] for i in page2["items"]}
    assert ids_page1.isdisjoint(ids_page2)


def test_status_filter_works(client):
    a = _submit_issue(client)
    _submit_issue(client)
    _advance(client, a["public_id"], "CLASSIFIED")

    response = client.get("/api/admin/issues", params={"status": "CLASSIFIED"})
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["public_id"] == a["public_id"]


def test_department_filter_uses_official_assignment_not_suggestion(client):
    """suggested_department is AI free text that never matches a real
    department code; department_code must filter by the OFFICIAL
    assignment only."""
    issue = _submit_issue(client, suggested_department="Roads Department (AI guess)")
    _advance(client, issue["public_id"], "CLASSIFIED")
    _assign(client, issue["public_id"], "STREET_LIGHTING")

    matching = client.get(
        "/api/admin/issues", params={"department_code": "STREET_LIGHTING"}
    ).json()
    assert matching["total"] == 1
    assert matching["items"][0]["public_id"] == issue["public_id"]

    not_matching = client.get(
        "/api/admin/issues", params={"department_code": "ELECTRICITY"}
    ).json()
    assert not_matching["total"] == 0


def test_severity_filter_works(client):
    _submit_issue(client, severity=SeverityLevel.HIGH)
    _submit_issue(client, severity=SeverityLevel.LOW)

    response = client.get("/api/admin/issues", params={"severity": "High"})
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["severity"] == "High"


def test_category_filter_works(client):
    _submit_issue(client, category=IssueCategory.STREET_LIGHTING)
    _submit_issue(client, category=IssueCategory.ROADS_AND_POTHOLES)

    response = client.get("/api/admin/issues", params={"category": "Roads and Potholes"})
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["category"] == "Roads and Potholes"


def test_multiple_filters_combine_with_and(client):
    target = _submit_issue(client, severity=SeverityLevel.CRITICAL)
    _advance(client, target["public_id"], "CLASSIFIED")
    _assign(client, target["public_id"], "STREET_LIGHTING")

    decoy = _submit_issue(client, severity=SeverityLevel.LOW)
    _advance(client, decoy["public_id"], "CLASSIFIED")
    _assign(client, decoy["public_id"], "STREET_LIGHTING")

    response = client.get(
        "/api/admin/issues",
        params={"department_code": "STREET_LIGHTING", "severity": "Critical"},
    )
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["public_id"] == target["public_id"]


def test_default_sorting_is_updated_at_descending(client):
    older = _submit_issue(client)
    newer = _submit_issue(client)
    _advance(client, newer["public_id"], "CLASSIFIED")  # bump updated_at again

    response = client.get("/api/admin/issues")
    public_ids = [item["public_id"] for item in response.json()["items"]]
    assert public_ids.index(newer["public_id"]) < public_ids.index(older["public_id"])


def test_explicit_sorting_works(client):
    first = _submit_issue(client)
    second = _submit_issue(client)

    response = client.get(
        "/api/admin/issues", params={"sort_by": "created_at", "sort_order": "asc"}
    )
    public_ids = [item["public_id"] for item in response.json()["items"]]
    assert public_ids.index(first["public_id"]) < public_ids.index(second["public_id"])


def test_admin_list_does_not_leak_internal_ids(client):
    issue = _submit_issue(client)
    _advance(client, issue["public_id"], "CLASSIFIED")
    _assign(client, issue["public_id"], "STREET_LIGHTING")

    body = client.get("/api/admin/issues").json()
    for item in body["items"]:
        assert "id" not in item
        assert "assigned_department_id" not in item
        if item["assigned_department"] is not None:
            assert "id" not in item["assigned_department"]


def test_admin_list_does_not_call_gemini(client):
    _submit_issue(client)
    with (
        patch("backend.main.analyze_complaint") as mock_main,
        patch("backend.service.analyze_complaint") as mock_service,
    ):
        response = client.get("/api/admin/issues")
    assert response.status_code == 200
    mock_main.assert_not_called()
    mock_service.assert_not_called()


# ============================================================================
# ADMIN DETAIL -- GET /api/admin/issues/{public_id}
# ============================================================================


def test_existing_issue_returns_full_authority_detail(client):
    created = _submit_issue(client)
    response = client.get(f"/api/admin/issues/{created['public_id']}")
    assert response.status_code == 200


def test_original_complaint_text_available_to_authority(client):
    text_value = "There has been no street light near our school for two weeks."
    created = _submit_issue(client, original_text=text_value)
    body = client.get(f"/api/admin/issues/{created['public_id']}").json()
    assert body["original_text"] == text_value


def test_suggested_department_available_to_authority(client):
    created = _submit_issue(client, suggested_department="Electrical / Street Lighting Department")
    body = client.get(f"/api/admin/issues/{created['public_id']}").json()
    assert body["suggested_department"] == "Electrical / Street Lighting Department"


def test_confidence_available_to_authority(client):
    created = _submit_issue(client, confidence=0.91)
    body = client.get(f"/api/admin/issues/{created['public_id']}").json()
    assert body["confidence"] == pytest.approx(0.91)


def test_current_assigned_department_available_in_detail(client):
    created = _submit_issue(client)
    _advance(client, created["public_id"], "CLASSIFIED")
    _assign(client, created["public_id"], "STREET_LIGHTING")

    body = client.get(f"/api/admin/issues/{created['public_id']}").json()
    assert body["assigned_department"] == {"code": "STREET_LIGHTING", "name": "Street Lighting"}


def test_status_history_included_in_detail(client):
    created = _submit_issue(client)
    _advance(client, created["public_id"], "CLASSIFIED")

    body = client.get(f"/api/admin/issues/{created['public_id']}").json()
    statuses = [(e["from_status"], e["to_status"]) for e in body["status_history"]]
    assert statuses == [(None, "SUBMITTED"), ("SUBMITTED", "CLASSIFIED")]


def test_assignment_history_included_in_detail(client):
    created = _submit_issue(client)
    _advance(client, created["public_id"], "CLASSIFIED")
    _assign(client, created["public_id"], "STREET_LIGHTING")
    _assign(client, created["public_id"], "ELECTRICITY")

    body = client.get(f"/api/admin/issues/{created['public_id']}").json()
    codes = [e["department_code"] for e in body["assignment_history"]]
    assert codes == ["STREET_LIGHTING", "ELECTRICITY"]


def test_internal_history_ids_excluded_from_detail(client):
    created = _submit_issue(client)
    _advance(client, created["public_id"], "CLASSIFIED")
    _assign(client, created["public_id"], "STREET_LIGHTING")

    body = client.get(f"/api/admin/issues/{created['public_id']}").json()
    assert "id" not in body
    for entry in body["status_history"]:
        assert "id" not in entry
        assert "issue_id" not in entry
    for entry in body["assignment_history"]:
        assert "id" not in entry
        assert "issue_id" not in entry
        assert "department_id" not in entry


def test_admin_detail_unknown_issue_returns_404(client):
    response = client.get("/api/admin/issues/CIV-AAAAAAAAAAAA")
    assert response.status_code == 404


def test_admin_detail_does_not_call_gemini(client):
    created = _submit_issue(client)
    with (
        patch("backend.main.analyze_complaint") as mock_main,
        patch("backend.service.analyze_complaint") as mock_service,
    ):
        response = client.get(f"/api/admin/issues/{created['public_id']}")
    assert response.status_code == 200
    mock_main.assert_not_called()
    mock_service.assert_not_called()


# ============================================================================
# DASHBOARD SUMMARY -- GET /api/admin/dashboard/summary
# ============================================================================


def test_dashboard_total_issues_correct(client):
    for _ in range(4):
        _submit_issue(client)
    body = client.get("/api/admin/dashboard/summary").json()
    assert body["total_issues"] == 4


def test_dashboard_status_counts_correct(client):
    a = _submit_issue(client)
    _submit_issue(client)
    _advance(client, a["public_id"], "CLASSIFIED")

    body = client.get("/api/admin/dashboard/summary").json()
    assert body["by_status"]["SUBMITTED"] == 1
    assert body["by_status"]["CLASSIFIED"] == 1


def test_dashboard_zero_count_statuses_included(client):
    _submit_issue(client)
    body = client.get("/api/admin/dashboard/summary").json()
    assert body["by_status"]["RESOLVED"] == 0
    assert body["by_status"]["CLOSED"] == 0
    assert body["by_status"]["REJECTED"] == 0
    assert set(body["by_status"].keys()) == {
        "SUBMITTED", "CLASSIFIED", "ROUTED", "ACKNOWLEDGED",
        "IN_PROGRESS", "RESOLVED", "CLOSED", "REJECTED", "REOPENED",
    }


def test_dashboard_severity_counts_correct(client):
    _submit_issue(client, severity=SeverityLevel.HIGH)
    _submit_issue(client, severity=SeverityLevel.HIGH)
    _submit_issue(client, severity=SeverityLevel.LOW)

    body = client.get("/api/admin/dashboard/summary").json()
    assert body["by_severity"]["High"] == 2
    assert body["by_severity"]["Low"] == 1
    assert body["by_severity"]["Critical"] == 0


def test_dashboard_department_counts_correct(client):
    a = _submit_issue(client)
    b = _submit_issue(client)
    for issue in (a, b):
        _advance(client, issue["public_id"], "CLASSIFIED")
    _assign(client, a["public_id"], "STREET_LIGHTING")
    _assign(client, b["public_id"], "STREET_LIGHTING")

    body = client.get("/api/admin/dashboard/summary").json()
    by_dept = {d["code"]: d["count"] for d in body["by_department"]}
    assert by_dept["STREET_LIGHTING"] == 2
    assert by_dept["ELECTRICITY"] == 0
    assert by_dept["ROADS_TRANSPORT"] == 0


def _advance_and_resolve(client, public_id, note="Repair crew fixed the reported issue on-site."):
    for status_value in ["CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS"]:
        r = client.post(f"/api/issues/{public_id}/status", json={"status": status_value})
        assert r.status_code == 200, r.json()
    r = client.post(f"/api/issues/{public_id}/resolve", json={"resolution_note": note})
    assert r.status_code == 200, r.json()
    return r.json()


def _request_reopen(client, public_id, reason="It broke again a week later."):
    r = client.post(f"/api/track/{public_id}/reopen-request", json={"reason": reason})
    assert r.status_code == 201, r.json()
    return r.json()


# --- Regression coverage: RESOLVED must not count as "active" -----------------
#
# Root cause of the Command Center / Departments / Civic Intelligence
# metric inconsistency: several independent aggregations (backend and, at
# the time, a duplicate copy in frontend/static/js/admin.js) excluded only
# CLOSED/REJECTED from "active", never RESOLVED. See
# backend.repository.METRIC_INACTIVE_STATUSES.


def test_dashboard_active_issue_count_excludes_resolved(client):
    """A RESOLVED issue must not be counted as active, even though it is
    not yet CLOSED."""
    resolved = _submit_issue(client)
    _advance_and_resolve(client, resolved["public_id"])

    routed = _submit_issue(client)
    _advance(client, routed["public_id"], "CLASSIFIED", "ROUTED")

    body = client.get("/api/admin/dashboard/summary").json()
    assert body["total_issues"] == 2
    assert body["active_issue_count"] == 1  # only the ROUTED one
    assert body["by_status"]["RESOLVED"] == 1  # RESOLVED is still reported, just not "active"


def test_dashboard_active_high_critical_count_excludes_resolved_critical(client):
    """Mirrors the exact reported scenario: a Critical issue that is
    RESOLVED must not inflate 'active High/Critical', while a High issue
    that is only ROUTED must."""
    resolved_critical = _submit_issue(client, severity=SeverityLevel.CRITICAL)
    _advance_and_resolve(client, resolved_critical["public_id"])

    routed_high = _submit_issue(client, severity=SeverityLevel.HIGH)
    _advance(client, routed_high["public_id"], "CLASSIFIED", "ROUTED")

    body = client.get("/api/admin/dashboard/summary").json()
    assert body["active_high_critical_count"] == 1
    assert body["by_severity"]["Critical"] == 1  # still reported in the raw breakdown
    assert body["by_severity"]["High"] == 1


def test_dashboard_active_high_critical_count_zero_when_only_resolved(client):
    resolved_critical = _submit_issue(client, severity=SeverityLevel.CRITICAL)
    _advance_and_resolve(client, resolved_critical["public_id"])

    body = client.get("/api/admin/dashboard/summary").json()
    assert body["active_high_critical_count"] == 0


def test_dashboard_active_issue_count_includes_reopened(client):
    """REOPENED is not a terminal state -- an issue back in REOPENED must
    count as active again."""
    resolved = _submit_issue(client)
    _advance_and_resolve(client, resolved["public_id"])
    reopen_request = _request_reopen(client, resolved["public_id"])
    decision = client.post(
        f"/api/admin/reopen-requests/{reopen_request['public_id']}/decision",
        json={"approve": True, "decision_reason": "Confirmed recurrence."},
    )
    assert decision.status_code == 200, decision.json()

    body = client.get("/api/admin/dashboard/summary").json()
    assert body["active_issue_count"] == 1
    assert body["by_status"]["REOPENED"] == 1


# --- Regression coverage: pending reopen-request badge on list views ----------


def test_admin_issue_list_item_has_pending_reopen_request_true_when_pending(client):
    resolved = _submit_issue(client)
    _advance_and_resolve(client, resolved["public_id"])
    _request_reopen(client, resolved["public_id"])

    body = client.get("/api/admin/issues").json()
    item = next(i for i in body["items"] if i["public_id"] == resolved["public_id"])
    assert item["has_pending_reopen_request"] is True
    assert item["status"] == "RESOLVED"  # status itself is untouched by the pending request


def test_admin_issue_list_item_has_pending_reopen_request_false_when_none(client):
    resolved = _submit_issue(client)
    _advance_and_resolve(client, resolved["public_id"])

    body = client.get("/api/admin/issues").json()
    item = next(i for i in body["items"] if i["public_id"] == resolved["public_id"])
    assert item["has_pending_reopen_request"] is False


def test_admin_issue_list_item_pending_reopen_flag_clears_after_approval(client):
    resolved = _submit_issue(client)
    _advance_and_resolve(client, resolved["public_id"])
    reopen_request = _request_reopen(client, resolved["public_id"])

    decision = client.post(
        f"/api/admin/reopen-requests/{reopen_request['public_id']}/decision",
        json={"approve": True, "decision_reason": None},
    )
    assert decision.status_code == 200, decision.json()

    body = client.get("/api/admin/issues").json()
    item = next(i for i in body["items"] if i["public_id"] == resolved["public_id"])
    # The request is no longer PENDING (it's APPROVED) -- the badge must clear.
    assert item["has_pending_reopen_request"] is False
    assert item["status"] == "REOPENED"


def test_admin_issue_list_item_pending_reopen_flag_clears_after_rejection(client):
    resolved = _submit_issue(client)
    _advance_and_resolve(client, resolved["public_id"])
    reopen_request = _request_reopen(client, resolved["public_id"])

    decision = client.post(
        f"/api/admin/reopen-requests/{reopen_request['public_id']}/decision",
        json={"approve": False, "decision_reason": "Insufficient evidence of recurrence."},
    )
    assert decision.status_code == 200, decision.json()

    body = client.get("/api/admin/issues").json()
    item = next(i for i in body["items"] if i["public_id"] == resolved["public_id"])
    assert item["has_pending_reopen_request"] is False
    assert item["status"] == "RESOLVED"  # rejection leaves the issue RESOLVED, not REOPENED


def test_admin_queue_list_item_also_carries_pending_reopen_flag(client):
    """The badge field must be present on the Issue Queue endpoint too, not
    just All Issues -- both share AdminIssueListItem."""
    resolved = _submit_issue(client)
    _advance_and_resolve(client, resolved["public_id"])
    _request_reopen(client, resolved["public_id"])

    body = client.get("/api/admin/queue").json()
    item = next((i for i in body["items"] if i["public_id"] == resolved["public_id"]), None)
    # RESOLVED is not in TERMINAL_STATUSES, so it's still in the operational
    # queue -- exactly so authorities can see and act on the reopen request.
    assert item is not None
    assert item["has_pending_reopen_request"] is True


def test_admin_issue_list_pending_reopen_flag_no_n_plus_1_query(client):
    """The bulk pending-reopen lookup must be one query for the whole page,
    not one per row."""
    for _ in range(5):
        issue = _submit_issue(client)
        _advance_and_resolve(client, issue["public_id"])

    executed = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        executed.append(statement)

    event.listen(client.engine, "before_cursor_execute", _record)
    try:
        response = client.get("/api/admin/issues")
    finally:
        event.remove(client.engine, "before_cursor_execute", _record)

    assert response.status_code == 200
    assert len(executed) <= 10


def _decide_reopen(client, request_public_id, approve, decision_reason=None):
    r = client.post(
        f"/api/admin/reopen-requests/{request_public_id}/decision",
        json={"approve": approve, "decision_reason": decision_reason},
    )
    assert r.status_code == 200, r.json()
    return r.json()


# --- Regression coverage: latest-reopen-request-state badge (Milestone 28.1) --
#
# A citizen may submit more than one reopen request over an issue's
# lifetime (e.g. rejected, then a later one approved). The badge must
# always reflect only the CURRENT/latest request, never a stale earlier
# one, while the full sequence stays in the existing status/decision
# history untouched. See backend.repository.get_latest_reopen_request_states.


def test_no_reopen_request_has_no_latest_state(client):
    resolved = _submit_issue(client)
    _advance_and_resolve(client, resolved["public_id"])

    body = client.get("/api/admin/issues").json()
    item = next(i for i in body["items"] if i["public_id"] == resolved["public_id"])
    assert item["latest_reopen_request_state"] is None
    assert item["has_pending_reopen_request"] is False


def test_pending_reopen_request_is_the_latest_state(client):
    resolved = _submit_issue(client)
    _advance_and_resolve(client, resolved["public_id"])
    _request_reopen(client, resolved["public_id"])

    body = client.get("/api/admin/issues").json()
    item = next(i for i in body["items"] if i["public_id"] == resolved["public_id"])
    assert item["latest_reopen_request_state"] == "PENDING"
    assert item["has_pending_reopen_request"] is True


def test_rejected_reopen_request_is_the_latest_state(client):
    resolved = _submit_issue(client)
    _advance_and_resolve(client, resolved["public_id"])
    req = _request_reopen(client, resolved["public_id"])
    _decide_reopen(client, req["public_id"], approve=False, decision_reason="Not enough evidence.")

    body = client.get("/api/admin/issues").json()
    item = next(i for i in body["items"] if i["public_id"] == resolved["public_id"])
    assert item["latest_reopen_request_state"] == "REJECTED"
    assert item["has_pending_reopen_request"] is False
    assert item["status"] == "RESOLVED"  # rejection never changes the issue's own status


def test_approved_reopen_request_is_the_latest_state_and_issue_reopens(client):
    resolved = _submit_issue(client)
    _advance_and_resolve(client, resolved["public_id"])
    req = _request_reopen(client, resolved["public_id"])
    _decide_reopen(client, req["public_id"], approve=True, decision_reason="Confirmed recurrence.")

    body = client.get("/api/admin/issues").json()
    item = next(i for i in body["items"] if i["public_id"] == resolved["public_id"])
    assert item["latest_reopen_request_state"] == "APPROVED"
    assert item["has_pending_reopen_request"] is False
    assert item["status"] == "REOPENED"  # existing approval workflow, unchanged


def test_rejected_then_new_pending_request_shows_only_pending(client):
    """The exact edge case in the spec: reject, then request again -- the
    latest state must be PENDING, not REJECTED, and there is only ever
    one current badge, never both."""
    resolved = _submit_issue(client)
    _advance_and_resolve(client, resolved["public_id"])

    first = _request_reopen(client, resolved["public_id"])
    _decide_reopen(client, first["public_id"], approve=False, decision_reason="Too soon to tell.")
    second = _request_reopen(client, resolved["public_id"], reason="It broke again, this time for real.")

    body = client.get("/api/admin/issues").json()
    item = next(i for i in body["items"] if i["public_id"] == resolved["public_id"])
    assert item["latest_reopen_request_state"] == "PENDING"
    assert item["has_pending_reopen_request"] is True

    detail = client.get(f"/api/admin/issues/{resolved['public_id']}").json()
    assert detail["latest_reopen_request_state"] == "PENDING"
    # The actionable panel must point at the NEW pending request, not the
    # old rejected one.
    assert detail["pending_reopen_request"]["public_id"] == second["public_id"]


def test_rejected_then_new_approved_request_shows_only_reopened(client):
    """Resolved -> Reopen Requested -> Rejected -> Reopen Requested again
    -> Approved -> Reopened. Final badge must be exactly [Reopened],
    never a stale Rejected or Pending badge."""
    resolved = _submit_issue(client)
    _advance_and_resolve(client, resolved["public_id"])

    first = _request_reopen(client, resolved["public_id"])
    _decide_reopen(client, first["public_id"], approve=False, decision_reason="Too soon to tell.")
    second = _request_reopen(client, resolved["public_id"], reason="It broke again, this time for real.")
    _decide_reopen(client, second["public_id"], approve=True, decision_reason="Confirmed on-site.")

    body = client.get("/api/admin/issues").json()
    item = next(i for i in body["items"] if i["public_id"] == resolved["public_id"])
    assert item["latest_reopen_request_state"] == "APPROVED"
    assert item["has_pending_reopen_request"] is False
    assert item["status"] == "REOPENED"

    detail = client.get(f"/api/admin/issues/{resolved['public_id']}").json()
    assert detail["latest_reopen_request_state"] == "APPROVED"
    assert detail["pending_reopen_request"] is None  # nothing currently actionable


def test_rejected_reopen_request_excluded_from_pending_metric(client):
    resolved = _submit_issue(client)
    _advance_and_resolve(client, resolved["public_id"])
    req = _request_reopen(client, resolved["public_id"])
    _decide_reopen(client, req["public_id"], approve=False, decision_reason="Not enough evidence.")

    body = client.get("/api/admin/analytics/resolution-intelligence").json()
    assert body["pending_reopen_requests"] == 0


def test_approved_reopen_request_excluded_from_pending_metric(client):
    resolved = _submit_issue(client)
    _advance_and_resolve(client, resolved["public_id"])
    req = _request_reopen(client, resolved["public_id"])
    _decide_reopen(client, req["public_id"], approve=True, decision_reason="Confirmed.")

    body = client.get("/api/admin/analytics/resolution-intelligence").json()
    assert body["pending_reopen_requests"] == 0


def test_new_pending_request_after_earlier_rejection_counted_as_pending(client):
    resolved = _submit_issue(client)
    _advance_and_resolve(client, resolved["public_id"])
    first = _request_reopen(client, resolved["public_id"])
    _decide_reopen(client, first["public_id"], approve=False, decision_reason="Too soon.")
    _request_reopen(client, resolved["public_id"], reason="It broke again.")

    body = client.get("/api/admin/analytics/resolution-intelligence").json()
    assert body["pending_reopen_requests"] == 1


def test_reopened_issue_counts_as_active_per_m28_metric(client):
    """M28 regression guard: REOPENED must remain active in the corrected
    active-issue metric (only RESOLVED/CLOSED/REJECTED are inactive)."""
    resolved = _submit_issue(client)
    _advance_and_resolve(client, resolved["public_id"])
    req = _request_reopen(client, resolved["public_id"])
    _decide_reopen(client, req["public_id"], approve=True, decision_reason="Confirmed.")

    summary = client.get("/api/admin/dashboard/summary").json()
    assert summary["by_status"]["REOPENED"] == 1
    assert summary["active_issue_count"] == 1  # the REOPENED issue is active


def test_reopen_request_history_preserved_across_reject_then_approve_cycle(client):
    """The historical sequence must remain fully intact in status history
    even though the badge only ever shows the current state."""
    resolved = _submit_issue(client)
    _advance_and_resolve(client, resolved["public_id"])
    first = _request_reopen(client, resolved["public_id"])
    _decide_reopen(client, first["public_id"], approve=False, decision_reason="Too soon.")
    second = _request_reopen(client, resolved["public_id"], reason="It broke again.")
    _decide_reopen(client, second["public_id"], approve=True, decision_reason="Confirmed.")

    detail = client.get(f"/api/admin/issues/{resolved['public_id']}").json()
    # The issue's own status history still records the real transition
    # sequence -- rejecting never adds a status-history row (it's not a
    # status change), approving does (RESOLVED -> REOPENED), matching the
    # existing, unmodified decide_reopen_request/transition mechanism.
    to_statuses = [row["to_status"] for row in detail["status_history"]]
    assert to_statuses[-1] == "REOPENED"
    assert "RESOLVED" in to_statuses


def test_latest_reopen_request_lookup_no_n_plus_1_query(client):
    """The bulk latest-state lookup must be one query for the whole page,
    regardless of how many issues have reopen request history."""
    for _ in range(5):
        issue = _submit_issue(client)
        _advance_and_resolve(client, issue["public_id"])
        req = _request_reopen(client, issue["public_id"])
        _decide_reopen(client, req["public_id"], approve=False, decision_reason="No.")
        _request_reopen(client, issue["public_id"], reason="Again.")

    executed = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        executed.append(statement)

    event.listen(client.engine, "before_cursor_execute", _record)
    try:
        response = client.get("/api/admin/issues")
    finally:
        event.remove(client.engine, "before_cursor_execute", _record)

    assert response.status_code == 200
    assert len(executed) <= 10


def test_dashboard_aggregation_is_database_driven_not_python_full_scan(client):
    """Regardless of dataset size, the summary should execute a small,
    constant number of SQL statements (a handful of GROUP BY queries),
    not one query per issue -- proof it's not a Python full-table count."""
    for _ in range(25):
        _submit_issue(client)

    executed = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        executed.append(statement)

    event.listen(client.engine, "before_cursor_execute", _record)
    try:
        response = client.get("/api/admin/dashboard/summary")
    finally:
        event.remove(client.engine, "before_cursor_execute", _record)

    assert response.status_code == 200
    # 3 GROUP BY queries (status, severity, department) plus small
    # constant overhead -- nowhere near one-per-issue for 25 issues.
    assert len(executed) <= 10


# ============================================================================
# DEPARTMENT SUMMARY -- GET /api/admin/departments/{department_code}/summary
# ============================================================================


def test_department_summary_returns_correct_department(client):
    body = client.get("/api/admin/departments/STREET_LIGHTING/summary").json()
    assert body["code"] == "STREET_LIGHTING"
    assert body["name"] == "Street Lighting"


def test_department_summary_total_assigned_issues_correct(client):
    a = _submit_issue(client)
    b = _submit_issue(client)
    for issue in (a, b):
        _advance(client, issue["public_id"], "CLASSIFIED")
    _assign(client, a["public_id"], "STREET_LIGHTING")
    _assign(client, b["public_id"], "ELECTRICITY")

    body = client.get("/api/admin/departments/STREET_LIGHTING/summary").json()
    assert body["total_assigned_issues"] == 1


def test_department_summary_status_counts_correct(client):
    a = _submit_issue(client)
    _advance(client, a["public_id"], "CLASSIFIED")
    _assign(client, a["public_id"], "STREET_LIGHTING")

    body = client.get("/api/admin/departments/STREET_LIGHTING/summary").json()
    assert body["by_status"]["ROUTED"] == 1
    assert body["by_status"]["CLASSIFIED"] == 0


def test_department_summary_severity_counts_correct(client):
    a = _submit_issue(client, severity=SeverityLevel.CRITICAL)
    _advance(client, a["public_id"], "CLASSIFIED")
    _assign(client, a["public_id"], "STREET_LIGHTING")

    body = client.get("/api/admin/departments/STREET_LIGHTING/summary").json()
    assert body["by_severity"]["Critical"] == 1
    assert body["by_severity"]["Low"] == 0


def test_department_summary_active_resolved_closed_counts(client):
    active_issue = _submit_issue(client)
    _advance(client, active_issue["public_id"], "CLASSIFIED")
    _assign(client, active_issue["public_id"], "STREET_LIGHTING")

    closed_issue = _submit_issue(client)
    _advance(client, closed_issue["public_id"], "CLASSIFIED")
    _assign(client, closed_issue["public_id"], "STREET_LIGHTING")
    _advance(
        client,
        closed_issue["public_id"],
        "ACKNOWLEDGED", "IN_PROGRESS", "RESOLVED", "CLOSED",
    )

    body = client.get("/api/admin/departments/STREET_LIGHTING/summary").json()
    assert body["active_issues"] == 1  # active_issue only (ROUTED)
    assert body["resolved_issues"] == 0  # closed_issue moved past RESOLVED into CLOSED
    assert body["closed_issues"] == 1


def test_department_summary_unknown_department_returns_404(client):
    response = client.get("/api/admin/departments/NOT_A_REAL_DEPARTMENT/summary")
    assert response.status_code == 404


# ============================================================================
# OPERATIONAL QUEUE -- GET /api/admin/queue
# ============================================================================


def test_queue_excludes_closed_and_rejected(client):
    closed = _submit_issue(client)
    _advance(
        client, closed["public_id"],
        "CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS", "RESOLVED", "CLOSED",
    )
    rejected = _submit_issue(client)
    _advance(client, rejected["public_id"], "REJECTED")

    body = client.get("/api/admin/queue").json()
    ids = {item["public_id"] for item in body["items"]}
    assert closed["public_id"] not in ids
    assert rejected["public_id"] not in ids


def test_queue_includes_operational_statuses(client):
    submitted = _submit_issue(client)
    in_progress = _submit_issue(client)
    _advance(client, in_progress["public_id"], "CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS")

    body = client.get("/api/admin/queue").json()
    ids = {item["public_id"] for item in body["items"]}
    assert submitted["public_id"] in ids
    assert in_progress["public_id"] in ids


def test_queue_orders_by_severity_priority(client):
    low = _submit_issue(client, severity=SeverityLevel.LOW)
    critical = _submit_issue(client, severity=SeverityLevel.CRITICAL)
    medium = _submit_issue(client, severity=SeverityLevel.MEDIUM)
    high = _submit_issue(client, severity=SeverityLevel.HIGH)
    unknown = _submit_issue(client, severity=SeverityLevel.UNKNOWN)

    body = client.get("/api/admin/queue").json()
    order = [item["public_id"] for item in body["items"]]
    expected = [
        critical["public_id"], high["public_id"], medium["public_id"],
        low["public_id"], unknown["public_id"],
    ]
    assert order == expected


def test_queue_orders_by_oldest_updated_at_within_equal_severity(client):
    first = _submit_issue(client, severity=SeverityLevel.HIGH)
    second = _submit_issue(client, severity=SeverityLevel.HIGH)

    body = client.get("/api/admin/queue").json()
    order = [item["public_id"] for item in body["items"]]
    assert order.index(first["public_id"]) < order.index(second["public_id"])


def test_queue_department_filter_works(client):
    a = _submit_issue(client)
    _advance(client, a["public_id"], "CLASSIFIED")
    _assign(client, a["public_id"], "STREET_LIGHTING")
    b = _submit_issue(client)  # unassigned, stays SUBMITTED

    body = client.get("/api/admin/queue", params={"department_code": "STREET_LIGHTING"}).json()
    ids = {item["public_id"] for item in body["items"]}
    assert a["public_id"] in ids
    assert b["public_id"] not in ids


def test_queue_pagination_works(client):
    for _ in range(5):
        _submit_issue(client)
    body = client.get("/api/admin/queue", params={"limit": 2, "offset": 0}).json()
    assert body["total"] == 5
    assert len(body["items"]) == 2


# ============================================================================
# STALE ISSUES -- GET /api/admin/issues/stale
# ============================================================================


def test_stale_default_48_hour_threshold_works(client):
    stale = _submit_issue(client)
    _backdate_updated_at(client, stale["public_id"], hours_ago=49)
    fresh = _submit_issue(client)

    body = client.get("/api/admin/issues/stale").json()
    ids = {item["public_id"] for item in body["items"]}
    assert stale["public_id"] in ids
    assert fresh["public_id"] not in ids


def test_stale_custom_threshold_works(client):
    issue = _submit_issue(client)
    _backdate_updated_at(client, issue["public_id"], hours_ago=2)

    not_stale_at_default = client.get("/api/admin/issues/stale").json()
    assert issue["public_id"] not in {i["public_id"] for i in not_stale_at_default["items"]}

    stale_at_custom = client.get(
        "/api/admin/issues/stale", params={"older_than_hours": 1}
    ).json()
    assert issue["public_id"] in {i["public_id"] for i in stale_at_custom["items"]}


def test_stale_department_filter_works(client):
    a = _submit_issue(client)
    _advance(client, a["public_id"], "CLASSIFIED")
    _assign(client, a["public_id"], "STREET_LIGHTING")
    _backdate_updated_at(client, a["public_id"], hours_ago=49)

    b = _submit_issue(client)
    _backdate_updated_at(client, b["public_id"], hours_ago=49)

    body = client.get(
        "/api/admin/issues/stale", params={"department_code": "STREET_LIGHTING"}
    ).json()
    ids = {item["public_id"] for item in body["items"]}
    assert a["public_id"] in ids
    assert b["public_id"] not in ids


def test_stale_status_filter_works(client):
    a = _submit_issue(client)
    _advance(client, a["public_id"], "CLASSIFIED")
    _backdate_updated_at(client, a["public_id"], hours_ago=49)

    b = _submit_issue(client)
    _backdate_updated_at(client, b["public_id"], hours_ago=49)

    body = client.get("/api/admin/issues/stale", params={"status": "CLASSIFIED"}).json()
    ids = {item["public_id"] for item in body["items"]}
    assert a["public_id"] in ids
    assert b["public_id"] not in ids


def test_stale_recent_issues_excluded(client):
    recent = _submit_issue(client)
    body = client.get("/api/admin/issues/stale").json()
    assert recent["public_id"] not in {i["public_id"] for i in body["items"]}


def test_stale_endpoint_causes_no_database_mutation(client):
    issue = _submit_issue(client)
    _backdate_updated_at(client, issue["public_id"], hours_ago=49)

    before = client.get(f"/api/issues/{issue['public_id']}").json()
    client.get("/api/admin/issues/stale")
    after = client.get(f"/api/issues/{issue['public_id']}").json()

    assert before == after
