"""
Milestone 18, Phase 5: Citizen Reopening.

Reuses the exact fixture patterns established in
tests/test_m18_phase3_evidence.py and test_m18_phase4_citizen_transparency.py:
a `client` fixture with get_current_authority overridden (for authority-
side setup/decision steps), and a `real_auth_client` fixture with the
REAL dependency for authorization tests. The citizen-facing endpoints
under test (POST /api/track/.../reopen-request) are genuinely public and
never go through that override -- called directly, unauthenticated.

Gemini is mocked throughout -- no real Gemini calls.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from ai.schemas import CivicIssue, IssueCategory, SeverityLevel
from backend.auth import get_current_authority, hash_password
from backend.database import Base, build_engine, get_db
from backend.main import app
from backend.models import Jurisdiction, JurisdictionLevel


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


def _build_test_app(tmp_path, db_name, *, real_auth=False):
    db_path = tmp_path / db_name
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    seed_db = TestingSessionLocal()
    seed_db.add(
        Jurisdiction(
            code="IN-WB-NADIA-KRISHNANAGAR", name="Krishnanagar Municipality",
            level=JurisdictionLevel.LOCAL_BODY, country_code="IN",
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
    if not real_auth:
        app.dependency_overrides[get_current_authority] = lambda: "test-authority"
    return TestClient(app), engine


@pytest.fixture
def client(tmp_path):
    test_client, engine = _build_test_app(tmp_path, "test_m18_phase5.db")
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


@pytest.fixture
def real_auth_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTHORITY_USERNAME", "reopen_authority")
    monkeypatch.setenv("AUTHORITY_PASSWORD_HASH", hash_password("m18-phase5-password"))
    monkeypatch.setenv("SESSION_SECRET", "m18-phase5-test-secret")
    test_client, engine = _build_test_app(tmp_path, "test_m18_phase5_auth.db", real_auth=True)
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


def _advance_and_resolve(client, public_id, note="Repair crew fixed the reported issue on-site."):
    for status_value in ["CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS"]:
        r = client.post(f"/api/issues/{public_id}/status", json={"status": status_value})
        assert r.status_code == 200
    r = client.post(f"/api/issues/{public_id}/resolve", json={"resolution_note": note})
    assert r.status_code == 200
    return r.json()


def _request_reopen(client, public_id, reason="The pothole has reappeared after only two weeks."):
    return client.post(f"/api/track/{public_id}/reopen-request", json={"reason": reason})


def _decide(client, request_public_id, approve, decision_reason=None):
    return client.post(
        f"/api/admin/reopen-requests/{request_public_id}/decision",
        json={"approve": approve, "decision_reason": decision_reason},
    )


# ============================================================================
# PUBLIC
# ============================================================================


def test_resolved_issue_can_submit_reopen_request(client):
    created = _submit_issue(client)
    _advance_and_resolve(client, created["public_id"])

    response = _request_reopen(client, created["public_id"])
    assert response.status_code == 201
    body = response.json()
    assert body["state"] == "PENDING"
    assert body["public_id"].startswith("RRQ-")
    assert body["reason"] == "The pothole has reappeared after only two weeks."


def test_reason_too_short_rejected(client):
    created = _submit_issue(client)
    _advance_and_resolve(client, created["public_id"])
    response = _request_reopen(client, created["public_id"], reason="x")
    assert response.status_code == 422


def test_whitespace_only_reason_rejected(client):
    created = _submit_issue(client)
    _advance_and_resolve(client, created["public_id"])
    response = _request_reopen(client, created["public_id"], reason="    ")
    assert response.status_code == 422


def test_missing_reason_field_rejected(client):
    created = _submit_issue(client)
    _advance_and_resolve(client, created["public_id"])
    response = client.post(f"/api/track/{created['public_id']}/reopen-request", json={})
    assert response.status_code == 422


def test_nonexistent_issue_rejected(client):
    response = _request_reopen(client, "CIV-AAAAAAAAAAAA")
    assert response.status_code == 404


def test_non_resolved_issue_rejected(client):
    created = _submit_issue(client)  # still SUBMITTED
    response = _request_reopen(client, created["public_id"])
    assert response.status_code == 400


@pytest.mark.parametrize("status_value", ["CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS"])
def test_non_resolved_intermediate_statuses_rejected(client, status_value):
    created = _submit_issue(client)
    public_id = created["public_id"]
    chain = ["CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS"]
    for s in chain[: chain.index(status_value) + 1]:
        client.post(f"/api/issues/{public_id}/status", json={"status": s})
    response = _request_reopen(client, public_id)
    assert response.status_code == 400


def test_duplicate_pending_request_rejected(client):
    created = _submit_issue(client)
    _advance_and_resolve(client, created["public_id"])
    first = _request_reopen(client, created["public_id"])
    assert first.status_code == 201

    second = _request_reopen(client, created["public_id"])
    assert second.status_code == 400


def test_citizen_cannot_manipulate_another_issue(client):
    """The reopen-request endpoint is keyed strictly to the public_id in
    the URL path -- there's no field anywhere in the request body that
    could reference a different issue."""
    issue_a = _submit_issue(client)
    issue_b = _submit_issue(client)
    _advance_and_resolve(client, issue_a["public_id"])
    _advance_and_resolve(client, issue_b["public_id"])

    _request_reopen(client, issue_a["public_id"])
    # issue_b must be completely unaffected -- it can still independently
    # accept its own reopen request.
    response = _request_reopen(client, issue_b["public_id"])
    assert response.status_code == 201


# ============================================================================
# AUTHORITY
# ============================================================================


def test_unauthenticated_authority_decision_endpoint_rejected(real_auth_client):
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()):
        created = real_auth_client.post("/api/issues", json={"text": "Some complaint."}).json()

    login = real_auth_client.post(
        "/api/authority/login",
        json={"username": "reopen_authority", "password": "m18-phase5-password"},
    )
    assert login.status_code == 200
    public_id = created["public_id"]
    for s in ["CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS"]:
        real_auth_client.post(f"/api/issues/{public_id}/status", json={"status": s})
    real_auth_client.post(f"/api/issues/{public_id}/resolve", json={"resolution_note": "Fixed."})
    request_response = real_auth_client.post(
        f"/api/track/{public_id}/reopen-request", json={"reason": "It broke again."}
    )
    request_id = request_response.json()["public_id"]
    real_auth_client.post("/api/authority/logout")

    response = real_auth_client.post(
        f"/api/admin/reopen-requests/{request_id}/decision", json={"approve": True}
    )
    assert response.status_code == 401


def test_pending_request_visible_to_authority_in_issue_detail(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _advance_and_resolve(client, public_id)
    _request_reopen(client, public_id)

    detail = client.get(f"/api/admin/issues/{public_id}").json()
    assert detail["pending_reopen_request"] is not None
    assert detail["pending_reopen_request"]["state"] == "PENDING"


def test_approve_works(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _advance_and_resolve(client, public_id)
    req = _request_reopen(client, public_id).json()

    response = _decide(client, req["public_id"], approve=True, decision_reason="Confirmed recurrence.")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "APPROVED"
    assert body["decision_reason"] == "Confirmed recurrence."
    assert body["decided_at"] is not None


def test_reject_works(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _advance_and_resolve(client, public_id)
    req = _request_reopen(client, public_id).json()

    response = _decide(client, req["public_id"], approve=False, decision_reason="Already fixed correctly.")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "REJECTED"
    assert body["decision_reason"] == "Already fixed correctly."


def test_invalid_request_state_rejected(client):
    """Deciding a nonexistent request_public_id must 404, not crash."""
    response = _decide(client, "RRQ-AAAAAAAAAAAA", approve=True)
    assert response.status_code == 404


def test_authority_decision_is_recorded(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _advance_and_resolve(client, public_id)
    req = _request_reopen(client, public_id).json()
    _decide(client, req["public_id"], approve=True)

    # No longer PENDING, so it must disappear from the "pending" slot in
    # the issue detail view.
    detail = client.get(f"/api/admin/issues/{public_id}").json()
    assert detail["pending_reopen_request"] is None


# ============================================================================
# LIFECYCLE
# ============================================================================


def test_approval_changes_issue_to_reopened(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _advance_and_resolve(client, public_id)
    req = _request_reopen(client, public_id).json()
    _decide(client, req["public_id"], approve=True)

    detail = client.get(f"/api/admin/issues/{public_id}").json()
    assert detail["status"] == "REOPENED"


def test_approval_creates_exactly_one_history_entry(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _advance_and_resolve(client, public_id)
    req = _request_reopen(client, public_id).json()

    before = len(client.get(f"/api/issues/{public_id}/history").json())
    _decide(client, req["public_id"], approve=True)
    after_history = client.get(f"/api/issues/{public_id}/history").json()

    assert len(after_history) == before + 1
    assert after_history[-1]["from_status"] == "RESOLVED"
    assert after_history[-1]["to_status"] == "REOPENED"


def test_rejection_leaves_issue_resolved(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _advance_and_resolve(client, public_id)
    req = _request_reopen(client, public_id).json()
    _decide(client, req["public_id"], approve=False)

    detail = client.get(f"/api/admin/issues/{public_id}").json()
    assert detail["status"] == "RESOLVED"


def test_rejection_creates_no_history_entry(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _advance_and_resolve(client, public_id)
    req = _request_reopen(client, public_id).json()

    before = len(client.get(f"/api/issues/{public_id}/history").json())
    _decide(client, req["public_id"], approve=False)
    after = len(client.get(f"/api/issues/{public_id}/history").json())
    assert after == before


def test_approval_fails_safely_if_issue_no_longer_resolved(client):
    """Simulates the issue being independently closed between the
    request being filed and the authority deciding on it."""
    created = _submit_issue(client)
    public_id = created["public_id"]
    _advance_and_resolve(client, public_id)
    req = _request_reopen(client, public_id).json()

    client.post(f"/api/issues/{public_id}/status", json={"status": "CLOSED"})
    response = _decide(client, req["public_id"], approve=True)
    assert response.status_code == 400

    # Request itself must remain untouched -- still PENDING.
    detail = client.get(f"/api/admin/issues/{public_id}").json()
    assert detail["pending_reopen_request"]["state"] == "PENDING"


def test_atomicity_persistence_failure_rolls_back_completely(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _advance_and_resolve(client, public_id)
    req = _request_reopen(client, public_id).json()

    db_gen = app.dependency_overrides[get_db]()
    db = next(db_gen)
    try:
        with patch.object(db, "commit", side_effect=SQLAlchemyError("simulated failure")):
            with pytest.raises(SQLAlchemyError):
                from backend.service import decide_reopen_request as service_decide

                service_decide(
                    db, req["public_id"], approve=True, decision_reason=None, decided_by="test-authority"
                )
        db.rollback()
    finally:
        db.close()

    detail = client.get(f"/api/admin/issues/{public_id}").json()
    assert detail["status"] == "RESOLVED"
    assert detail["pending_reopen_request"]["state"] == "PENDING"


def test_existing_resolution_workflow_still_works(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    result = _advance_and_resolve(client, public_id)
    assert result["status"] == "RESOLVED"
    assert result["resolution_summary"] is not None


# ============================================================================
# REGRESSION
# ============================================================================


def test_regression_tracking_api_correct_without_reopen_request(client):
    created = _submit_issue(client)
    tracked = client.get(f"/api/track/{created['public_id']}").json()
    assert tracked["active_reopen_request"] is None


def test_regression_evidence_visibility_unaffected(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _advance_and_resolve(client, public_id)

    tracked = client.get(f"/api/track/{public_id}").json()
    assert tracked["evidence"] == []
    assert tracked["resolution_summary"] is not None


def test_regression_resolved_by_still_protected(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _advance_and_resolve(client, public_id)
    _request_reopen(client, public_id)

    tracked = client.get(f"/api/track/{public_id}").json()
    assert "resolved_by" not in tracked
    assert "test-authority" not in str(tracked)
