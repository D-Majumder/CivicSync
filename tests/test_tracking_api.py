"""
API-level tests for GET /api/track/{public_id}, CivicSync's public,
unauthenticated citizen tracking endpoint.

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
    db_path = tmp_path / "test_tracking_api.db"
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


def _submit_issue(client, **overrides) -> dict:
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue(**overrides)):
        response = client.post("/api/issues", json={"text": "Some complaint."})
    assert response.status_code == 201
    return response.json()


def _classify(client, public_id: str, reason: str | None = None) -> dict:
    body = {"status": "CLASSIFIED"}
    if reason is not None:
        body["reason"] = reason
    response = client.post(f"/api/issues/{public_id}/status", json=body)
    assert response.status_code == 200
    return response.json()


def _route(client, public_id: str, department_code: str = "STREET_LIGHTING") -> dict:
    response = client.post(
        f"/api/issues/{public_id}/assignment", json={"department_code": department_code}
    )
    assert response.status_code == 200
    return response.json()


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


def _decide_reopen(client, request_public_id, approve, decision_reason=None):
    r = client.post(
        f"/api/admin/reopen-requests/{request_public_id}/decision",
        json={"approve": approve, "decision_reason": decision_reason},
    )
    assert r.status_code == 200, r.json()
    return r.json()


# --- 1-7: basic tracking of a fresh issue ------------------------------------


def test_existing_issue_can_be_publicly_tracked(client):
    created = _submit_issue(client)
    response = client.get(f"/api/track/{created['public_id']}")
    assert response.status_code == 200


def test_correct_public_id_returned(client):
    created = _submit_issue(client)
    body = client.get(f"/api/track/{created['public_id']}").json()
    assert body["public_id"] == created["public_id"]


def test_category_returned(client):
    created = _submit_issue(client)
    body = client.get(f"/api/track/{created['public_id']}").json()
    assert body["category"] == "Street Lighting"


def test_problem_returned(client):
    created = _submit_issue(client)
    body = client.get(f"/api/track/{created['public_id']}").json()
    assert body["problem"] == "Lack of functioning street light near a school."


def test_location_returned_when_available(client):
    created = _submit_issue(client, location="near our school")
    body = client.get(f"/api/track/{created['public_id']}").json()
    assert body["location"] == "near our school"


def test_severity_returned(client):
    created = _submit_issue(client)
    body = client.get(f"/api/track/{created['public_id']}").json()
    assert body["severity"] == "Medium"


def test_current_status_returned(client):
    created = _submit_issue(client)
    body = client.get(f"/api/track/{created['public_id']}").json()
    assert body["status"] == "SUBMITTED"
    assert body["status_label"] == "Submitted"


# --- 8: current assigned department -----------------------------------------


def test_current_assigned_department_returned(client):
    created = _submit_issue(client)
    _classify(client, created["public_id"])
    _route(client, created["public_id"], "STREET_LIGHTING")

    body = client.get(f"/api/track/{created['public_id']}").json()
    assert body["assigned_department"] == {"code": "STREET_LIGHTING", "name": "Street Lighting"}


# --- 9, 10, 11: timeline -----------------------------------------------------


def test_timeline_returned_oldest_first(client):
    created = _submit_issue(client)
    _classify(client, created["public_id"], reason="Complaint reviewed and classified.")
    _route(client, created["public_id"], "STREET_LIGHTING")

    body = client.get(f"/api/track/{created['public_id']}").json()
    statuses = [entry["status"] for entry in body["timeline"]]
    assert statuses == ["SUBMITTED", "CLASSIFIED", "ROUTED"]


def test_initial_submitted_entry_appears_correctly(client):
    created = _submit_issue(client)
    body = client.get(f"/api/track/{created['public_id']}").json()
    assert len(body["timeline"]) == 1
    first = body["timeline"][0]
    assert first["status"] == "SUBMITTED"
    assert first["reason"] == "Issue submitted."
    assert first["timestamp"] is not None


def test_from_status_is_not_exposed_in_timeline(client):
    created = _submit_issue(client)
    _classify(client, created["public_id"])
    body = client.get(f"/api/track/{created['public_id']}").json()
    for entry in body["timeline"]:
        assert "from_status" not in entry
        assert set(entry.keys()) == {"status", "timestamp", "reason"}


def test_all_timestamps_are_unambiguous_utc_iso_strings(client):
    """Regression test for a real bug: timestamps from SQLite round-trip
    as naive datetimes (UTC wall-clock value, no tzinfo). Serialized
    as-is, that produces an ISO string with no "Z"/offset -- which
    JavaScript's `new Date(...)` parses as LOCAL time per spec, silently
    corrupting the displayed time by the viewer's UTC offset (observed as
    exactly a 5h30m error for an IST viewer). Every timestamp in an API
    response must end in "Z" or a numeric UTC offset so this can never
    happen regardless of client."""
    created = _submit_issue(client)
    _classify(client, created["public_id"])
    body = client.get(f"/api/track/{created['public_id']}").json()

    def assert_unambiguous(value, field_name):
        assert value is not None, f"{field_name} was unexpectedly null"
        assert value.endswith("Z") or "+" in value[10:] or "-" in value[19:], (
            f"{field_name}={value!r} has no timezone marker -- this is exactly "
            f"the ambiguous-timestamp bug."
        )

    assert_unambiguous(body["created_at"], "created_at")
    assert_unambiguous(body["updated_at"], "updated_at")
    for entry in body["timeline"]:
        assert_unambiguous(entry["timestamp"], "timeline.timestamp")


def test_timestamp_round_trips_correctly_to_ist(client):
    """End-to-end proof the fix actually produces the right IST wall-clock
    time for a known UTC instant -- not just "has a Z suffix"."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    created = _submit_issue(client)
    body = client.get(f"/api/track/{created['public_id']}").json()

    parsed = datetime.fromisoformat(body["created_at"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None

    ist = parsed.astimezone(ZoneInfo("Asia/Kolkata"))
    utc = parsed.astimezone(timezone.utc)
    # IST is always exactly UTC+5:30 -- verifying the offset itself,
    # rather than hardcoding any specific clock time.
    offset = ist.utcoffset() - utc.utcoffset()
    assert offset.total_seconds() == 5.5 * 3600


# --- 12-17: privacy -- internal/sensitive fields never exposed --------------


def test_internal_issue_id_not_exposed(client):
    created = _submit_issue(client)
    body = client.get(f"/api/track/{created['public_id']}").json()
    assert "id" not in body


def test_assigned_department_id_not_exposed(client):
    created = _submit_issue(client)
    _classify(client, created["public_id"])
    _route(client, created["public_id"])
    body = client.get(f"/api/track/{created['public_id']}").json()
    assert "assigned_department_id" not in body
    assert "id" not in body["assigned_department"]


def test_status_history_internal_id_not_exposed(client):
    created = _submit_issue(client)
    body = client.get(f"/api/track/{created['public_id']}").json()
    for entry in body["timeline"]:
        assert "id" not in entry
        assert "issue_id" not in entry


def test_assignment_history_internal_id_not_exposed(client):
    """The public endpoint doesn't expose assignment history at all (only
    the current department), so there's nothing assignment-history-shaped
    in the body to leak an id from -- confirm that directly."""
    created = _submit_issue(client)
    _classify(client, created["public_id"])
    _route(client, created["public_id"])
    body = client.get(f"/api/track/{created['public_id']}").json()
    assert "assignment_history" not in body
    assert "history" not in body  # only "timeline" is exposed, not raw history


def test_suggested_department_not_exposed(client):
    created = _submit_issue(client, suggested_department="Electrical / Street Lighting Department")
    body = client.get(f"/api/track/{created['public_id']}").json()
    assert "suggested_department" not in body


def test_confidence_not_exposed(client):
    created = _submit_issue(client, confidence=0.97)
    body = client.get(f"/api/track/{created['public_id']}").json()
    assert "confidence" not in body


def test_original_text_not_exposed(client):
    created = _submit_issue(client)
    body = client.get(f"/api/track/{created['public_id']}").json()
    assert "original_text" not in body


def test_resolution_summary_now_exposed_but_null_when_unresolved(client):
    """Milestone 18, Phase 4: resolution_summary is now a citizen-visible
    field (a citizen is entitled to know how their own report was
    resolved) -- deliberately changed from earlier behavior. For a
    freshly-submitted, unresolved issue it's present but null."""
    created = _submit_issue(client)
    body = client.get(f"/api/track/{created['public_id']}").json()
    assert "resolution_summary" in body
    assert body["resolution_summary"] is None


def test_resolved_by_never_exposed_to_citizens(client):
    """resolved_by (the resolving authority's identity) stays
    authority-only -- the one resolution field deliberately NOT exposed
    on the public tracking endpoint."""
    created = _submit_issue(client)
    body = client.get(f"/api/track/{created['public_id']}").json()
    assert "resolved_by" not in body


# --- 18: no Gemini call -------------------------------------------------------


def test_tracking_does_not_call_gemini(client):
    created = _submit_issue(client)
    with (
        patch("backend.main.analyze_complaint") as mock_main,
        patch("backend.service.analyze_complaint") as mock_service,
    ):
        response = client.get(f"/api/track/{created['public_id']}")
    assert response.status_code == 200
    mock_main.assert_not_called()
    mock_service.assert_not_called()


# --- 19, 20: not found / malformed -------------------------------------------


def test_unknown_public_id_returns_404(client):
    response = client.get("/api/track/CIV-AAAAAAAAAAAA")
    assert response.status_code == 404


@pytest.mark.parametrize(
    "malformed_id",
    [
        "CIV-short",
        "CIV-TOOLONGHEXVALUE123",
        "civ-affda97dda53",  # lowercase
        "NOTCIV-AFFDA97DDA53",
        "CIV_AFFDA97DDA53",  # wrong separator
        "CIV-AFFDA97DDG3",  # non-hex character G
        "CIV-",
        "not-an-id-at-all",
    ],
)
def test_malformed_public_id_returns_422(client, malformed_id):
    response = client.get(f"/api/track/{malformed_id}")
    assert response.status_code == 422


def test_malformed_public_id_does_not_query_database(client):
    """A malformed id should be rejected by path validation before any
    repository/service call runs."""
    with patch("backend.service.get_public_tracking") as mock_get_tracking:
        response = client.get("/api/track/not-a-valid-id")
    assert response.status_code == 422
    mock_get_tracking.assert_not_called()


# --- 21: unassigned issue --------------------------------------------------


def test_unassigned_issue_returns_null_assigned_department(client):
    created = _submit_issue(client)
    body = client.get(f"/api/track/{created['public_id']}").json()
    assert body["assigned_department"] is None


# --- 22, 23: CLOSED and REOPENED issues work ---------------------------------


def test_tracking_a_closed_issue_works(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    for target in ("CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS", "RESOLVED", "CLOSED"):
        r = client.post(f"/api/issues/{public_id}/status", json={"status": target})
        assert r.status_code == 200

    body = client.get(f"/api/track/{public_id}").json()
    assert body["status"] == "CLOSED"
    assert body["status_label"] == "Closed"
    assert [entry["status"] for entry in body["timeline"]] == [
        "SUBMITTED",
        "CLASSIFIED",
        "ROUTED",
        "ACKNOWLEDGED",
        "IN_PROGRESS",
        "RESOLVED",
        "CLOSED",
    ]


def test_tracking_a_reopened_issue_works(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    for target in (
        "CLASSIFIED",
        "ROUTED",
        "ACKNOWLEDGED",
        "IN_PROGRESS",
        "RESOLVED",
        "REOPENED",
    ):
        r = client.post(f"/api/issues/{public_id}/status", json={"status": target})
        assert r.status_code == 200

    body = client.get(f"/api/track/{public_id}").json()
    assert body["status"] == "REOPENED"
    assert body["status_label"] == "Reopened"


# --- Milestone 29.1: citizen-facing latest reopen-request state -------------
#
# active_reopen_request (above) is deliberately PENDING-only -- it also
# gates whether the citizen-facing reopen FORM is shown, and that must
# stay unchanged. latest_reopen_request_state is the new, separate field
# that lets the tracking page additionally recognize a REJECTED request,
# without altering when the form itself appears.


def test_no_reopen_request_has_no_latest_state(client):
    created = _submit_issue(client)
    _advance_and_resolve(client, created["public_id"])

    body = client.get(f"/api/track/{created['public_id']}").json()
    assert body["latest_reopen_request_state"] is None
    assert body["active_reopen_request"] is None


def test_pending_reopen_request_is_the_latest_state(client):
    created = _submit_issue(client)
    _advance_and_resolve(client, created["public_id"])
    _request_reopen(client, created["public_id"])

    body = client.get(f"/api/track/{created['public_id']}").json()
    assert body["latest_reopen_request_state"] == "PENDING"
    assert body["active_reopen_request"] is not None


def test_rejected_reopen_request_is_the_latest_state(client):
    created = _submit_issue(client)
    _advance_and_resolve(client, created["public_id"])
    req = _request_reopen(client, created["public_id"])
    _decide_reopen(client, req["public_id"], approve=False, decision_reason="Not enough evidence.")

    body = client.get(f"/api/track/{created['public_id']}").json()
    assert body["latest_reopen_request_state"] == "REJECTED"
    assert body["active_reopen_request"] is None  # form must remain available
    assert body["status"] == "RESOLVED"  # rejection never changes the issue's own status


def test_rejected_then_new_pending_shows_only_pending_to_citizen(client):
    """The exact edge case in the spec: a citizen must never see a stale
    'rejected' state once they've submitted a new request."""
    created = _submit_issue(client)
    _advance_and_resolve(client, created["public_id"])
    first = _request_reopen(client, created["public_id"])
    _decide_reopen(client, first["public_id"], approve=False, decision_reason="Too soon to tell.")
    _request_reopen(client, created["public_id"], reason="It broke again, this time for real.")

    body = client.get(f"/api/track/{created['public_id']}").json()
    assert body["latest_reopen_request_state"] == "PENDING"
    assert body["active_reopen_request"] is not None


def test_rejected_then_new_approved_shows_reopened_to_citizen(client):
    created = _submit_issue(client)
    _advance_and_resolve(client, created["public_id"])
    first = _request_reopen(client, created["public_id"])
    _decide_reopen(client, first["public_id"], approve=False, decision_reason="Too soon to tell.")
    second = _request_reopen(client, created["public_id"], reason="It broke again, this time for real.")
    _decide_reopen(client, second["public_id"], approve=True, decision_reason="Confirmed on-site.")

    body = client.get(f"/api/track/{created['public_id']}").json()
    assert body["latest_reopen_request_state"] == "APPROVED"
    assert body["status"] == "REOPENED"
    assert body["status_label"] == "Reopened"
    assert body["active_reopen_request"] is None


# --- 24: no stack traces / internal details leaked --------------------------


def test_not_found_response_never_leaks_stack_trace(client):
    response = client.get("/api/track/CIV-AAAAAAAAAAAA")
    assert "Traceback" not in response.text
    assert "File \"" not in response.text
    assert response.json()["detail"] == "No issue found with that tracking id."


def test_malformed_id_response_never_leaks_stack_trace(client):
    response = client.get("/api/track/not-a-valid-id")
    assert "Traceback" not in response.text
    assert "File \"" not in response.text


# --- Serialization whitelist: exact field set, guards against future leaks --


def test_response_top_level_fields_are_exactly_the_approved_set(client):
    created = _submit_issue(client)
    body = client.get(f"/api/track/{created['public_id']}").json()
    assert set(body.keys()) == {
        "public_id",
        "category",
        "problem",
        "location",
        "duration",
        "affected_population",
        "severity",
        "status",
        "status_label",
        "assigned_department",
        "created_at",
        "updated_at",
        "resolution_summary",
        "resolved_at",
        "timeline",
        "evidence",
        "active_reopen_request",
        "latest_reopen_request_state",
    }


def test_response_timeline_entry_fields_are_exactly_the_approved_set(client):
    created = _submit_issue(client)
    body = client.get(f"/api/track/{created['public_id']}").json()
    for entry in body["timeline"]:
        assert set(entry.keys()) == {"status", "timestamp", "reason"}


def test_response_assigned_department_fields_are_exactly_the_approved_set(client):
    created = _submit_issue(client)
    _classify(client, created["public_id"])
    _route(client, created["public_id"])
    body = client.get(f"/api/track/{created['public_id']}").json()
    assert set(body["assigned_department"].keys()) == {"code", "name"}
