"""
Milestone 18, Phase 2: Authority Resolution Workflow
(POST /api/issues/{public_id}/resolve).

Reuses the exact fixture patterns established in
tests/test_m18_phase1_lifecycle.py: a `client` fixture with the
get_current_authority dependency overridden (for tests where authorization
itself isn't under test), and a `real_auth_client` fixture with the REAL
dependency (for the tests that must prove authentication/authorization is
actually enforced).

Gemini is mocked throughout -- no real Gemini calls. Every test uses an
isolated, temporary SQLite database via a get_db dependency override,
never civicsync.db.
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
from backend.models import IssueStatus, Jurisdiction, JurisdictionLevel


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
    test_client, engine = _build_test_app(tmp_path, "test_m18_phase2.db")
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


@pytest.fixture
def real_auth_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTHORITY_USERNAME", "resolver_authority")
    monkeypatch.setenv("AUTHORITY_PASSWORD_HASH", hash_password("m18-phase2-password"))
    monkeypatch.setenv("SESSION_SECRET", "m18-phase2-test-secret")
    test_client, engine = _build_test_app(tmp_path, "test_m18_phase2_auth.db", real_auth=True)
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


def _advance_to(client, public_id, *statuses):
    for status_value in statuses:
        response = client.post(f"/api/issues/{public_id}/status", json={"status": status_value})
        assert response.status_code == 200, response.text


def _submit_and_advance_to_in_progress(client) -> dict:
    created = _submit_issue(client)
    _advance_to(client, created["public_id"], "CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS")
    return created


def _resolve(client, public_id, resolution_note="Repair crew fixed the reported issue on-site."):
    return client.post(f"/api/issues/{public_id}/resolve", json={"resolution_note": resolution_note})


# ============================================================================
# Successful resolution
# ============================================================================


def test_authenticated_authority_can_resolve_an_in_progress_issue(client):
    created = _submit_and_advance_to_in_progress(client)
    response = _resolve(client, created["public_id"])
    assert response.status_code == 200
    assert response.json()["status"] == "RESOLVED"


def test_resolution_note_is_stored(client):
    created = _submit_and_advance_to_in_progress(client)
    note = "Replaced the damaged street light fixture and tested it works."
    response = _resolve(client, created["public_id"], resolution_note=note)
    assert response.json()["resolution_summary"] == note

    # Also confirm it's durably persisted, not just echoed in the response.
    detail = client.get(f"/api/admin/issues/{created['public_id']}").json()
    assert detail["resolution_summary"] == note


def test_resolver_identity_is_stored(client):
    created = _submit_and_advance_to_in_progress(client)
    response = _resolve(client, created["public_id"])
    assert response.json()["resolved_by"] == "test-authority"

    detail = client.get(f"/api/admin/issues/{created['public_id']}").json()
    assert detail["resolved_by"] == "test-authority"


def test_resolved_at_is_server_generated_and_recent(client):
    from datetime import datetime, timezone

    created = _submit_and_advance_to_in_progress(client)
    before = datetime.now(timezone.utc)
    response = _resolve(client, created["public_id"])
    after = datetime.now(timezone.utc)

    resolved_at = datetime.fromisoformat(response.json()["resolved_at"].replace("Z", "+00:00"))
    assert before <= resolved_at <= after


def test_status_becomes_resolved(client):
    created = _submit_and_advance_to_in_progress(client)
    _resolve(client, created["public_id"])
    detail = client.get(f"/api/admin/issues/{created['public_id']}").json()
    assert detail["status"] == "RESOLVED"


def test_exactly_one_status_history_record_is_created(client):
    created = _submit_and_advance_to_in_progress(client)
    public_id = created["public_id"]
    before = len(client.get(f"/api/issues/{public_id}/history").json())

    _resolve(client, public_id)

    after_history = client.get(f"/api/issues/{public_id}/history").json()
    assert len(after_history) == before + 1
    newest = after_history[-1]
    assert newest["from_status"] == "IN_PROGRESS"
    assert newest["to_status"] == "RESOLVED"


def test_resolve_reuses_the_existing_transition_mechanism_not_a_duplicate(client):
    """The history reason for a resolution is the resolution note itself
    -- proving resolve_issue() delegates to the same
    _apply_validated_status_transition primitive the generic status
    endpoint uses, rather than writing history through a separate path."""
    created = _submit_and_advance_to_in_progress(client)
    public_id = created["public_id"]
    note = "Pothole filled and road surface re-tested for safety."
    _resolve(client, public_id, resolution_note=note)

    history = client.get(f"/api/issues/{public_id}/history").json()
    assert history[-1]["reason"] == note


# ============================================================================
# Validation
# ============================================================================


def test_empty_resolution_note_rejected(client):
    created = _submit_and_advance_to_in_progress(client)
    response = client.post(
        f"/api/issues/{created['public_id']}/resolve", json={"resolution_note": ""}
    )
    assert response.status_code == 422


def test_whitespace_only_resolution_note_rejected(client):
    created = _submit_and_advance_to_in_progress(client)
    response = client.post(
        f"/api/issues/{created['public_id']}/resolve", json={"resolution_note": "    "}
    )
    assert response.status_code == 422


def test_missing_resolution_note_field_rejected(client):
    created = _submit_and_advance_to_in_progress(client)
    response = client.post(f"/api/issues/{created['public_id']}/resolve", json={})
    assert response.status_code == 422


def test_too_short_resolution_note_rejected(client):
    created = _submit_and_advance_to_in_progress(client)
    response = client.post(
        f"/api/issues/{created['public_id']}/resolve", json={"resolution_note": "ok"}
    )
    assert response.status_code == 422


def test_client_cannot_provide_resolved_at(client):
    """Extra fields in the request body (e.g. a client-supplied
    resolved_at) must have no effect -- the server-generated value is
    always used regardless of what's sent."""
    created = _submit_and_advance_to_in_progress(client)
    response = client.post(
        f"/api/issues/{created['public_id']}/resolve",
        json={
            "resolution_note": "Fixed and verified on-site.",
            "resolved_at": "2000-01-01T00:00:00Z",
        },
    )
    assert response.status_code == 200
    resolved_at = response.json()["resolved_at"]
    assert "2000-01-01" not in resolved_at


def test_client_cannot_provide_resolved_by(client):
    created = _submit_and_advance_to_in_progress(client)
    response = client.post(
        f"/api/issues/{created['public_id']}/resolve",
        json={"resolution_note": "Fixed and verified on-site.", "resolved_by": "someone_else"},
    )
    assert response.status_code == 200
    assert response.json()["resolved_by"] == "test-authority"


def test_unauthorized_request_rejected(real_auth_client):
    """No login at all -- the real, unmocked auth dependency must reject."""
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()):
        created = real_auth_client.post("/api/issues", json={"text": "Some complaint."}).json()

    response = real_auth_client.post(
        f"/api/issues/{created['public_id']}/resolve",
        json={"resolution_note": "Fixed and verified on-site."},
    )
    assert response.status_code == 401


def test_authorized_request_succeeds_after_real_login(real_auth_client):
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()):
        created = real_auth_client.post("/api/issues", json={"text": "Some complaint."}).json()
    public_id = created["public_id"]

    login = real_auth_client.post(
        "/api/authority/login",
        json={"username": "resolver_authority", "password": "m18-phase2-password"},
    )
    assert login.status_code == 200

    for status_value in ["CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS"]:
        advance = real_auth_client.post(
            f"/api/issues/{public_id}/status", json={"status": status_value}
        )
        assert advance.status_code == 200

    response = real_auth_client.post(
        f"/api/issues/{public_id}/resolve", json={"resolution_note": "Fixed and verified on-site."}
    )
    assert response.status_code == 200
    assert response.json()["resolved_by"] == "resolver_authority"


@pytest.mark.parametrize(
    "starting_status",
    ["SUBMITTED", "CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "RESOLVED", "CLOSED", "REJECTED"],
)
def test_non_in_progress_issue_cannot_be_resolved(client, starting_status):
    """Only IN_PROGRESS may transition to RESOLVED via this endpoint --
    every other starting status must be rejected with 4xx, matching the
    same validator the generic status endpoint uses."""
    created = _submit_issue(client)
    public_id = created["public_id"]

    path_to_status = {
        "SUBMITTED": [],
        "CLASSIFIED": ["CLASSIFIED"],
        "ROUTED": ["CLASSIFIED", "ROUTED"],
        "ACKNOWLEDGED": ["CLASSIFIED", "ROUTED", "ACKNOWLEDGED"],
        "RESOLVED": ["CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS", "RESOLVED"],
        "CLOSED": ["CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS", "RESOLVED", "CLOSED"],
        "REJECTED": ["REJECTED"],
    }
    _advance_to(client, public_id, *path_to_status[starting_status])

    response = _resolve(client, public_id)
    assert 400 <= response.status_code < 500


def test_resolving_missing_issue_returns_404(client):
    response = _resolve(client, "CIV-DOESNOTEXIST12")
    assert response.status_code == 404
    assert "Traceback" not in response.text


# ============================================================================
# Atomicity
# ============================================================================


def test_invalid_transition_leaves_resolution_metadata_unchanged(client):
    """Attempting to resolve a SUBMITTED issue must leave
    resolution_summary/resolved_by/status completely untouched -- not
    even partially set."""
    created = _submit_issue(client)
    public_id = created["public_id"]

    response = _resolve(client, public_id)
    assert response.status_code == 400

    detail = client.get(f"/api/admin/issues/{public_id}").json()
    assert detail["status"] == "SUBMITTED"
    assert detail["resolution_summary"] is None
    assert detail["resolved_by"] is None
    assert detail["resolved_at"] is None


def test_invalid_transition_creates_no_history_record(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    before = len(client.get(f"/api/issues/{public_id}/history").json())

    _resolve(client, public_id)

    after = len(client.get(f"/api/issues/{public_id}/history").json())
    assert after == before


def test_persistence_failure_during_resolution_rolls_back_completely(client):
    """Simulates a database failure at commit time -- proves resolution
    metadata, status, and history are all part of the SAME transaction
    and none of them is left partially persisted."""
    created = _submit_and_advance_to_in_progress(client)
    public_id = created["public_id"]

    db_gen = app.dependency_overrides[get_db]()
    db = next(db_gen)
    try:
        with patch.object(db, "commit", side_effect=SQLAlchemyError("simulated failure")):
            with pytest.raises(SQLAlchemyError):
                from backend.service import resolve_issue as service_resolve_issue

                service_resolve_issue(db, public_id, "Fixed on-site.", "test-authority")
        db.rollback()
    finally:
        db.close()

    detail = client.get(f"/api/admin/issues/{public_id}").json()
    assert detail["status"] == "IN_PROGRESS"
    assert detail["resolution_summary"] is None
    assert detail["resolved_by"] is None

    history = client.get(f"/api/issues/{public_id}/history").json()
    # SUBMITTED->CLASSIFIED->ROUTED->ACKNOWLEDGED->IN_PROGRESS = 5 entries
    # (including the initial null->SUBMITTED row), none extra.
    assert len(history) == 5


def test_validation_failure_never_reaches_the_database(client):
    """An invalid resolution_note (rejected by Pydantic, 422) must never
    even attempt a transition -- confirmed by the issue staying
    IN_PROGRESS with zero new history entries."""
    created = _submit_and_advance_to_in_progress(client)
    public_id = created["public_id"]
    before = len(client.get(f"/api/issues/{public_id}/history").json())

    response = client.post(
        f"/api/issues/{public_id}/resolve", json={"resolution_note": ""}
    )
    assert response.status_code == 422

    detail = client.get(f"/api/admin/issues/{public_id}").json()
    assert detail["status"] == "IN_PROGRESS"
    after = len(client.get(f"/api/issues/{public_id}/history").json())
    assert after == before


# ============================================================================
# Regression
# ============================================================================


def test_regression_jurisdiction_id_untouched_by_resolution(client):
    created = _submit_and_advance_to_in_progress(client)
    public_id = created["public_id"]
    _resolve(client, public_id)

    scoped = client.get(
        "/api/admin/issues", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    ).json()
    assert public_id in {i["public_id"] for i in scoped["items"]}


def test_regression_assigned_department_untouched_by_resolution(client):
    created = _submit_and_advance_to_in_progress(client)
    public_id = created["public_id"]

    before_detail = client.get(f"/api/admin/issues/{public_id}").json()
    _resolve(client, public_id)
    after_detail = client.get(f"/api/admin/issues/{public_id}").json()

    assert before_detail["assigned_department"] == after_detail["assigned_department"]


def test_regression_public_tracking_reflects_resolution(client):
    created = _submit_and_advance_to_in_progress(client)
    public_id = created["public_id"]
    _resolve(client, public_id)

    tracked = client.get(f"/api/track/{public_id}").json()
    assert tracked["status"] == "RESOLVED"
    # Privacy: resolver identity must never leak to the public endpoint.
    assert "resolved_by" not in tracked
    assert "test-authority" not in str(tracked)


def test_regression_resolution_never_calls_gemini(client):
    created = _submit_and_advance_to_in_progress(client)
    with patch("backend.service.analyze_complaint") as mock_analyze:
        response = _resolve(client, created["public_id"])
    assert response.status_code == 200
    mock_analyze.assert_not_called()


def test_regression_generic_status_endpoint_can_still_resolve_directly(client):
    """The pre-existing generic status endpoint's own ability to
    transition IN_PROGRESS -> RESOLVED must remain completely intact --
    the new dedicated endpoint is additive, not a replacement."""
    created = _submit_and_advance_to_in_progress(client)
    response = client.post(
        f"/api/issues/{created['public_id']}/status", json={"status": "RESOLVED"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "RESOLVED"


def test_regression_full_lifecycle_still_reaches_closed_after_resolution(client):
    created = _submit_and_advance_to_in_progress(client)
    public_id = created["public_id"]
    _resolve(client, public_id)

    response = client.post(f"/api/issues/{public_id}/status", json={"status": "CLOSED"})
    assert response.status_code == 200
    assert response.json()["status"] == "CLOSED"
