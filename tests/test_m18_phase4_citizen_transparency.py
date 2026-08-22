"""
Milestone 18, Phase 4: Citizen Resolution Transparency.

Reuses the exact fixture patterns established in
tests/test_m18_phase3_evidence.py: a `client` fixture with
get_current_authority overridden (for authority-side setup steps like
uploading evidence and resolving), and isolated per-test evidence
storage. The tracking endpoints under test here (GET /api/track/...)
are genuinely public and never go through that override -- they're
called directly, unauthenticated, exactly as a citizen would.

Gemini is mocked throughout -- no real Gemini calls.
"""

from io import BytesIO
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import sessionmaker

from ai.schemas import CivicIssue, IssueCategory, SeverityLevel
from backend.auth import get_current_authority
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


def _real_png_bytes(color="red") -> bytes:
    buf = BytesIO()
    Image.new("RGB", (20, 20), color=color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CIVICSYNC_EVIDENCE_STORAGE_DIR", str(tmp_path / "evidence_storage"))
    db_path = tmp_path / "test_m18_phase4.db"
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
    app.dependency_overrides[get_current_authority] = lambda: "test-authority"
    test_client = TestClient(app)
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


def _advance_to_in_progress(client, public_id):
    for status_value in ["CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS"]:
        r = client.post(f"/api/issues/{public_id}/status", json={"status": status_value})
        assert r.status_code == 200


def _upload_evidence(client, public_id, color="red") -> dict:
    response = client.post(
        f"/api/issues/{public_id}/evidence",
        files={"file": ("site_photo.png", _real_png_bytes(color=color), "image/png")},
    )
    assert response.status_code == 201
    return response.json()


def _resolve(client, public_id, note="Repair crew fixed the reported issue on-site."):
    response = client.post(f"/api/issues/{public_id}/resolve", json={"resolution_note": note})
    assert response.status_code == 200
    return response.json()


# ============================================================================
# Citizen can see evidence metadata for their tracked issue
# ============================================================================


def test_citizen_sees_evidence_metadata_for_tracked_issue(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _advance_to_in_progress(client, public_id)
    uploaded = _upload_evidence(client, public_id)

    tracked = client.get(f"/api/track/{public_id}").json()
    assert len(tracked["evidence"]) == 1
    entry = tracked["evidence"][0]
    assert entry["public_id"] == uploaded["public_id"]
    assert entry["original_filename"] == "site_photo.png"
    assert entry["uploaded_at"] is not None


def test_evidence_metadata_excludes_sensitive_fields(client):
    """No internal id, no storage_key, no uploader identity, no
    filesystem path -- only the minimal citizen-safe metadata."""
    created = _submit_issue(client)
    public_id = created["public_id"]
    _advance_to_in_progress(client, public_id)
    _upload_evidence(client, public_id)

    entry = client.get(f"/api/track/{public_id}").json()["evidence"][0]
    assert set(entry.keys()) == {"public_id", "original_filename", "uploaded_at"}
    assert "storage_key" not in entry
    assert "uploaded_by" not in entry
    assert "id" not in entry


# ============================================================================
# Citizen can retrieve/view evidence belonging to that issue
# ============================================================================


def test_citizen_can_retrieve_evidence_belonging_to_tracked_issue(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _advance_to_in_progress(client, public_id)
    content = _real_png_bytes(color="green")
    upload_response = client.post(
        f"/api/issues/{public_id}/evidence",
        files={"file": ("photo.png", content, "image/png")},
    )
    evidence_id = upload_response.json()["public_id"]

    response = client.get(f"/api/track/{public_id}/evidence/{evidence_id}/file")
    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"] == "image/png"


def test_evidence_retrieval_via_track_route_requires_no_authentication(client):
    """The public retrieval route must work with zero auth -- no cookie,
    no login -- exactly like every other citizen tracking call."""
    created = _submit_issue(client)
    public_id = created["public_id"]
    _advance_to_in_progress(client, public_id)
    uploaded = _upload_evidence(client, public_id)

    fresh_client = TestClient(app)  # no cookies at all
    response = fresh_client.get(f"/api/track/{public_id}/evidence/{uploaded['public_id']}/file")
    assert response.status_code == 200


# ============================================================================
# Citizen cannot retrieve evidence belonging to another issue
# ============================================================================


def test_citizen_cannot_retrieve_evidence_belonging_to_another_issue(client):
    issue_a = _submit_issue(client)
    issue_b = _submit_issue(client)
    _advance_to_in_progress(client, issue_a["public_id"])
    evidence_a = _upload_evidence(client, issue_a["public_id"])

    # Correct evidence_public_id, but requested under the WRONG issue's
    # tracking id -- must be rejected, not silently served.
    response = client.get(
        f"/api/track/{issue_b['public_id']}/evidence/{evidence_a['public_id']}/file"
    )
    assert response.status_code == 404


def test_cross_issue_attempt_does_not_leak_via_response_content(client):
    issue_a = _submit_issue(client)
    issue_b = _submit_issue(client)
    _advance_to_in_progress(client, issue_a["public_id"])
    evidence_a = _upload_evidence(client, issue_a["public_id"], color="blue")

    response = client.get(
        f"/api/track/{issue_b['public_id']}/evidence/{evidence_a['public_id']}/file"
    )
    assert response.status_code == 404
    assert "Traceback" not in response.text


# ============================================================================
# Nonexistent tracking ID / issue without evidence
# ============================================================================


def test_nonexistent_tracking_id_returns_404_for_evidence_list(client):
    response = client.get("/api/track/CIV-AAAAAAAAAAAA")
    assert response.status_code == 404


def test_nonexistent_tracking_id_returns_404_for_evidence_file(client):
    response = client.get("/api/track/CIV-AAAAAAAAAAAA/evidence/EVD-AAAAAAAAAAAA/file")
    assert response.status_code == 404


def test_issue_without_evidence_returns_empty_list_not_error(client):
    created = _submit_issue(client)
    tracked = client.get(f"/api/track/{created['public_id']}").json()
    assert tracked["evidence"] == []


def test_nonexistent_evidence_id_on_real_issue_returns_404(client):
    created = _submit_issue(client)
    response = client.get(f"/api/track/{created['public_id']}/evidence/EVD-DOESNOTEXIST1/file")
    assert response.status_code == 404


# ============================================================================
# Resolved issue exposes resolution note/timestamp appropriately
# ============================================================================


def test_resolved_issue_exposes_resolution_note_and_timestamp(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _advance_to_in_progress(client, public_id)
    _resolve(client, public_id, note="Pothole filled and road surface re-tested.")

    tracked = client.get(f"/api/track/{public_id}").json()
    assert tracked["status"] == "RESOLVED"
    assert tracked["resolution_summary"] == "Pothole filled and road surface re-tested."
    assert tracked["resolved_at"] is not None


def test_unresolved_issue_has_null_resolution_fields(client):
    created = _submit_issue(client)
    tracked = client.get(f"/api/track/{created['public_id']}").json()
    assert tracked["resolution_summary"] is None
    assert tracked["resolved_at"] is None


# ============================================================================
# resolved_by is not leaked to citizen responses
# ============================================================================


def test_resolved_by_not_leaked_on_resolved_issue(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _advance_to_in_progress(client, public_id)
    _resolve(client, public_id)

    tracked = client.get(f"/api/track/{public_id}").json()
    assert "resolved_by" not in tracked
    assert "test-authority" not in str(tracked)


def test_uploader_identity_not_leaked_in_evidence_list(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _advance_to_in_progress(client, public_id)
    _upload_evidence(client, public_id)

    tracked = client.get(f"/api/track/{public_id}").json()
    assert "test-authority" not in str(tracked["evidence"])


# ============================================================================
# Authority upload endpoint remains protected
# ============================================================================


def test_authority_upload_endpoint_still_requires_authentication():
    """A completely fresh, unauthenticated client (no dependency
    override at all) must still be rejected -- confirms Phase 4 didn't
    weaken the existing authority-only upload boundary."""
    import os

    from backend.auth import hash_password

    os.environ["AUTHORITY_USERNAME"] = "phase4_check_authority"
    os.environ["AUTHORITY_PASSWORD_HASH"] = hash_password("irrelevant")
    os.environ.setdefault("SESSION_SECRET", "phase4-check-secret")

    fresh_client = TestClient(app)
    response = fresh_client.post(
        "/api/issues/CIV-AAAAAAAAAAAA/evidence",
        files={"file": ("photo.png", _real_png_bytes(), "image/png")},
    )
    assert response.status_code == 401


def test_authority_evidence_list_endpoint_still_requires_authentication():
    fresh_client = TestClient(app)
    response = fresh_client.get("/api/issues/CIV-AAAAAAAAAAAA/evidence")
    assert response.status_code == 401


def test_authority_evidence_file_endpoint_still_requires_authentication():
    fresh_client = TestClient(app)
    response = fresh_client.get("/api/evidence/EVD-AAAAAAAAAAAA/file")
    assert response.status_code == 401


# ============================================================================
# Existing evidence functionality still works
# ============================================================================


def test_existing_authority_evidence_upload_still_works(client):
    created = _submit_issue(client)
    response = client.post(
        f"/api/issues/{created['public_id']}/evidence",
        files={"file": ("photo.png", _real_png_bytes(), "image/png")},
    )
    assert response.status_code == 201


def test_existing_authority_evidence_list_still_works(client):
    created = _submit_issue(client)
    _upload_evidence(client, created["public_id"])
    response = client.get(f"/api/issues/{created['public_id']}/evidence")
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_existing_resolve_endpoint_unaffected_by_public_evidence_route(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _advance_to_in_progress(client, public_id)
    response = client.post(
        f"/api/issues/{public_id}/resolve", json={"resolution_note": "Fixed on-site."}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "RESOLVED"


# ============================================================================
# Malformed/unauthorized access is rejected safely
# ============================================================================


def test_malformed_tracking_id_returns_422(client):
    response = client.get("/api/track/not-a-valid-id")
    assert response.status_code == 422


def test_path_traversal_style_evidence_id_returns_404_not_500(client):
    created = _submit_issue(client)
    response = client.get(f"/api/track/{created['public_id']}/evidence/..%2F..%2Fetc%2Fpasswd/file")
    assert response.status_code in (404, 422)
    assert "Traceback" not in response.text


def test_empty_evidence_id_does_not_crash(client):
    created = _submit_issue(client)
    response = client.get(f"/api/track/{created['public_id']}/evidence//file")
    assert response.status_code in (404, 422)
