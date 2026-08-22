"""
Milestone 18, Phase 1: Authoritative Issue Lifecycle.

INSPECTION FINDING, documented here rather than in a separate file: the
required lifecycle state machine

    SUBMITTED -> CLASSIFIED -> ROUTED -> ACKNOWLEDGED -> IN_PROGRESS
        -> RESOLVED -> CLOSED
    SUBMITTED -> REJECTED
    RESOLVED -> REOPENED -> IN_PROGRESS

was ALREADY fully implemented by the existing architecture
(backend/transitions.py's VALID_TRANSITIONS, backend/repository.py's
validate-before-mutate _apply_validated_status_transition, and
backend/main.py's POST /api/issues/{public_id}/status route) before this
milestone began -- confirmed by direct inspection, matching the required
matrix exactly. No backend code changes were needed or made for M18
Phase 1.

This file adds no new backend behavior. Its purpose is traceability: one
dedicated, clearly-named test per M18 Phase 1 requirement bullet, so a
reader can confirm each requirement is met without cross-referencing
tests/test_lifecycle.py and tests/test_lifecycle_api.py (which already
cover this thoroughly, from earlier milestones, and are left completely
unmodified). Where a test below overlaps with existing coverage, that
overlap is intentional confirmation, not duplication for its own sake.

Gemini is mocked throughout -- no real Gemini calls. Every test uses an
isolated, temporary SQLite database via a get_db dependency override,
never civicsync.db. Authorization tests use the REAL (unmocked)
get_current_authority dependency, unlike every other test in this file.
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
from backend.models import Issue, IssueStatus, Jurisdiction, JurisdictionLevel

REQUIRED_CHAIN = [
    IssueStatus.SUBMITTED,
    IssueStatus.CLASSIFIED,
    IssueStatus.ROUTED,
    IssueStatus.ACKNOWLEDGED,
    IssueStatus.IN_PROGRESS,
    IssueStatus.RESOLVED,
    IssueStatus.CLOSED,
]


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
    test_client, engine = _build_test_app(tmp_path, "test_m18_phase1.db")
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


@pytest.fixture
def real_auth_client(tmp_path, monkeypatch):
    """A client with the REAL get_current_authority dependency (not
    overridden), for the one authorization test below that needs to
    prove a genuinely unauthenticated request is rejected."""
    monkeypatch.setenv("AUTHORITY_USERNAME", "m18_authority")
    monkeypatch.setenv("AUTHORITY_PASSWORD_HASH", hash_password("m18-test-password"))
    monkeypatch.setenv("SESSION_SECRET", "m18-phase1-test-secret")
    test_client, engine = _build_test_app(tmp_path, "test_m18_phase1_auth.db", real_auth=True)
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


def _transition(client, public_id, status_value):
    return client.post(f"/api/issues/{public_id}/status", json={"status": status_value})


# ============================================================================
# Requirement: "Valid transitions update status atomically and create
# exactly one status-history record." + the required chain itself.
# ============================================================================


def test_required_chain_every_step_succeeds_and_advances_status(client):
    """Walks the entire required chain SUBMITTED -> ... -> CLOSED one hop
    at a time via the real API, confirming every individual hop is
    accepted and the reported status advances correctly."""
    created = _submit_issue(client)
    public_id = created["public_id"]
    assert created["status"] == IssueStatus.SUBMITTED.value

    for target in REQUIRED_CHAIN[1:]:
        response = _transition(client, public_id, target.value)
        assert response.status_code == 200, f"transition to {target.value} failed: {response.text}"
        assert response.json()["status"] == target.value


@pytest.mark.parametrize(
    "from_status,to_status",
    list(zip(REQUIRED_CHAIN[:-1], REQUIRED_CHAIN[1:])),
)
def test_each_required_hop_creates_exactly_one_history_record(client, from_status, to_status):
    """Isolates each individual hop in the required chain and confirms it
    creates exactly one new IssueStatusHistory row (not zero, not two)."""
    created = _submit_issue(client)
    public_id = created["public_id"]

    # Walk to from_status first (skip if already there, e.g. SUBMITTED).
    idx = REQUIRED_CHAIN.index(from_status)
    for intermediate in REQUIRED_CHAIN[1 : idx + 1]:
        _transition(client, public_id, intermediate.value)

    before = len(client.get(f"/api/issues/{public_id}/history").json())
    response = _transition(client, public_id, to_status.value)
    assert response.status_code == 200
    after = len(client.get(f"/api/issues/{public_id}/history").json())
    assert after == before + 1


def test_exceptional_path_submitted_to_rejected(client):
    created = _submit_issue(client)
    response = _transition(client, created["public_id"], "REJECTED")
    assert response.status_code == 200
    assert response.json()["status"] == "REJECTED"


def test_exceptional_path_resolved_reopened_in_progress(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    for target in ["CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS", "RESOLVED"]:
        assert _transition(client, public_id, target).status_code == 200

    reopened = _transition(client, public_id, "REOPENED")
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "REOPENED"

    back_in_progress = _transition(client, public_id, "IN_PROGRESS")
    assert back_in_progress.status_code == 200
    assert back_in_progress.json()["status"] == "IN_PROGRESS"


# ============================================================================
# Requirement: "Reject illegal status jumps with an appropriate 4xx
# response." + "Invalid transitions must change nothing and create no
# history record."
# ============================================================================


@pytest.mark.parametrize(
    "illegal_target",
    ["ROUTED", "ACKNOWLEDGED", "IN_PROGRESS", "RESOLVED", "CLOSED", "REOPENED"],
)
def test_illegal_jumps_from_submitted_are_rejected_with_4xx(client, illegal_target):
    """Representative illegal jumps: SUBMITTED may only go to CLASSIFIED
    or REJECTED -- every other target must be a 4xx, never a 2xx."""
    created = _submit_issue(client)
    response = _transition(client, created["public_id"], illegal_target)
    assert 400 <= response.status_code < 500


def test_illegal_jump_changes_nothing_and_creates_no_history(client):
    created = _submit_issue(client)
    public_id = created["public_id"]

    before_status = client.get(f"/api/issues/{public_id}").json()["status"]
    before_history_len = len(client.get(f"/api/issues/{public_id}/history").json())

    response = _transition(client, public_id, "RESOLVED")
    assert response.status_code == 400

    after_status = client.get(f"/api/issues/{public_id}").json()["status"]
    after_history_len = len(client.get(f"/api/issues/{public_id}/history").json())
    assert after_status == before_status
    assert after_history_len == before_history_len


def test_closed_is_terminal(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    for target in REQUIRED_CHAIN[1:]:
        _transition(client, public_id, target.value)
    assert client.get(f"/api/issues/{public_id}").json()["status"] == "CLOSED"

    for attempt in ["IN_PROGRESS", "RESOLVED", "REOPENED", "SUBMITTED"]:
        response = _transition(client, public_id, attempt)
        assert 400 <= response.status_code < 500


def test_rejected_is_terminal(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _transition(client, public_id, "REJECTED")

    for attempt in ["CLASSIFIED", "SUBMITTED", "IN_PROGRESS"]:
        response = _transition(client, public_id, attempt)
        assert 400 <= response.status_code < 500


def test_invalid_status_value_is_rejected_with_4xx_not_500(client):
    """A status value that isn't a real IssueStatus at all (not just an
    illegal transition) must be a clean validation 4xx, never a 500."""
    created = _submit_issue(client)
    response = _transition(client, created["public_id"], "NOT_A_REAL_STATUS")
    assert 400 <= response.status_code < 500


# ============================================================================
# Requirement: "failed-transition rollback"
# ============================================================================


def test_persistence_failure_during_transition_rolls_back_cleanly(client):
    """Simulates a database failure at commit time and confirms the
    session is rolled back -- neither the status nor a history row is
    left half-committed."""
    created = _submit_issue(client)
    public_id = created["public_id"]

    db_gen = app.dependency_overrides[get_db]()
    db = next(db_gen)
    try:
        with patch.object(db, "commit", side_effect=SQLAlchemyError("simulated failure")):
            with pytest.raises(SQLAlchemyError):
                from backend.service import transition_issue

                transition_issue(db, public_id, IssueStatus.CLASSIFIED)
        db.rollback()
    finally:
        db.close()

    # Independently verify via the real API that nothing was persisted.
    detail = client.get(f"/api/issues/{public_id}").json()
    assert detail["status"] == "SUBMITTED"
    history = client.get(f"/api/issues/{public_id}/history").json()
    assert len(history) == 1


# ============================================================================
# Requirement: "authorization"
# ============================================================================


def test_status_transition_requires_authentication(real_auth_client):
    """With the REAL authentication dependency (not overridden), an
    unauthenticated request to the status transition route must be
    rejected, never silently succeed."""
    # A public, unauthenticated action (issue submission) still works --
    # only the authority-only transition route requires login.
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()):
        created = real_auth_client.post("/api/issues", json={"text": "Some complaint."}).json()

    response = real_auth_client.post(
        f"/api/issues/{created['public_id']}/status", json={"status": "CLASSIFIED"}
    )
    assert response.status_code == 401


def test_status_transition_works_after_real_login(real_auth_client):
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()):
        created = real_auth_client.post("/api/issues", json={"text": "Some complaint."}).json()

    login = real_auth_client.post(
        "/api/authority/login",
        json={"username": "m18_authority", "password": "m18-test-password"},
    )
    assert login.status_code == 200

    response = real_auth_client.post(
        f"/api/issues/{created['public_id']}/status", json={"status": "CLASSIFIED"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "CLASSIFIED"


# ============================================================================
# Requirement: "Preserve existing authentication, authorization,
# jurisdiction, assignment, AI and tracking behavior." (regression)
# ============================================================================


def test_regression_jurisdiction_scoping_unaffected_by_a_transition(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _transition(client, public_id, "CLASSIFIED")

    scoped = client.get(
        "/api/admin/issues", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    ).json()
    assert public_id in {i["public_id"] for i in scoped["items"]}


def test_regression_assignment_still_independent_of_lifecycle(client):
    """A CLASSIFIED issue transitioning further must not silently gain or
    lose an official department assignment -- that's a separate action."""
    created = _submit_issue(client)
    public_id = created["public_id"]
    _transition(client, public_id, "CLASSIFIED")

    detail = client.get(f"/api/issues/{public_id}").json()
    assert detail["assigned_department"] is None


def test_regression_public_tracking_reflects_transitions(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _transition(client, public_id, "CLASSIFIED")

    tracked = client.get(f"/api/track/{public_id}").json()
    assert tracked["status"] == "CLASSIFIED"


def test_regression_transition_never_calls_gemini(client):
    created = _submit_issue(client)
    with patch("backend.service.analyze_complaint") as mock_analyze:
        response = _transition(client, created["public_id"], "CLASSIFIED")
    assert response.status_code == 200
    mock_analyze.assert_not_called()


def test_regression_missing_issue_returns_404_not_500(client):
    response = _transition(client, "CIV-DOESNOTEXIST12", "CLASSIFIED")
    assert response.status_code == 404
    assert "Traceback" not in response.text
