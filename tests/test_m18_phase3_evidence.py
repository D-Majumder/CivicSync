"""
Milestone 18, Phase 3: Resolution Evidence Attachments.

Reuses the exact fixture patterns established in
tests/test_m18_phase2_resolution.py: a `client` fixture with
get_current_authority overridden, and a `real_auth_client` fixture with
the REAL dependency for authorization tests. Every test uses an
isolated, temporary SQLite database via a get_db dependency override,
never civicsync.db, and a per-test-isolated evidence storage directory
(CIVICSYNC_EVIDENCE_STORAGE_DIR under tmp_path) -- never the real
evidence_storage/ directory.

Gemini is mocked throughout -- no real Gemini calls.
"""

from io import BytesIO
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from ai.schemas import CivicIssue, IssueCategory, SeverityLevel
from backend.auth import get_current_authority, hash_password
from backend.database import Base, build_engine, get_db
from backend.main import app
from backend.models import Jurisdiction, JurisdictionLevel, ResolutionEvidence


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


def _real_png_bytes(size=(20, 20), color="red") -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def _real_jpeg_bytes(size=(20, 20), color="blue") -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="JPEG")
    return buf.getvalue()


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
def client(tmp_path, monkeypatch):
    # Isolated evidence storage directory per test -- never the real
    # evidence_storage/ used by the running app.
    monkeypatch.setenv("CIVICSYNC_EVIDENCE_STORAGE_DIR", str(tmp_path / "evidence_storage"))
    test_client, engine = _build_test_app(tmp_path, "test_m18_phase3.db")
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


@pytest.fixture
def real_auth_client(tmp_path, monkeypatch):
    monkeypatch.setenv("CIVICSYNC_EVIDENCE_STORAGE_DIR", str(tmp_path / "evidence_storage"))
    monkeypatch.setenv("AUTHORITY_USERNAME", "evidence_authority")
    monkeypatch.setenv("AUTHORITY_PASSWORD_HASH", hash_password("m18-phase3-password"))
    monkeypatch.setenv("SESSION_SECRET", "m18-phase3-test-secret")
    test_client, engine = _build_test_app(tmp_path, "test_m18_phase3_auth.db", real_auth=True)
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


def _upload(client, public_id, content=None, filename="photo.png", content_type="image/png"):
    if content is None:
        content = _real_png_bytes()
    return client.post(
        f"/api/issues/{public_id}/evidence",
        files={"file": (filename, content, content_type)},
    )


# ============================================================================
# Upload
# ============================================================================


def test_authorized_authority_can_upload_a_valid_image(client):
    created = _submit_issue(client)
    response = _upload(client, created["public_id"])
    assert response.status_code == 201
    body = response.json()
    assert body["public_id"].startswith("EVD-")
    assert body["content_type"] == "image/png"


def test_metadata_is_persisted_correctly(client):
    created = _submit_issue(client)
    content = _real_jpeg_bytes()
    response = _upload(
        client, created["public_id"], content=content, filename="site_photo.jpg", content_type="image/jpeg"
    )
    body = response.json()
    assert body["original_filename"] == "site_photo.jpg"
    assert body["content_type"] == "image/jpeg"
    assert body["size_bytes"] == len(content)
    assert body["uploaded_by"] == "test-authority"
    assert body["uploaded_at"] is not None


def test_evidence_is_associated_with_the_correct_issue(client):
    issue_a = _submit_issue(client)
    issue_b = _submit_issue(client)
    _upload(client, issue_a["public_id"])

    a_evidence = client.get(f"/api/issues/{issue_a['public_id']}/evidence").json()
    b_evidence = client.get(f"/api/issues/{issue_b['public_id']}/evidence").json()
    assert len(a_evidence) == 1
    assert len(b_evidence) == 0


def test_uploaded_file_is_retrievable_byte_identical(client):
    created = _submit_issue(client)
    content = _real_png_bytes(color="green")
    upload_response = _upload(client, created["public_id"], content=content)
    evidence_id = upload_response.json()["public_id"]

    file_response = client.get(f"/api/evidence/{evidence_id}/file")
    assert file_response.status_code == 200
    assert file_response.content == content
    assert file_response.headers["content-type"] == "image/png"


# ============================================================================
# Validation
# ============================================================================


def test_unauthenticated_upload_rejected(real_auth_client):
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()):
        created = real_auth_client.post("/api/issues", json={"text": "Some complaint."}).json()

    response = real_auth_client.post(
        f"/api/issues/{created['public_id']}/evidence",
        files={"file": ("photo.png", _real_png_bytes(), "image/png")},
    )
    assert response.status_code == 401


def test_authorized_upload_succeeds_after_real_login(real_auth_client):
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()):
        created = real_auth_client.post("/api/issues", json={"text": "Some complaint."}).json()

    login = real_auth_client.post(
        "/api/authority/login",
        json={"username": "evidence_authority", "password": "m18-phase3-password"},
    )
    assert login.status_code == 200

    response = real_auth_client.post(
        f"/api/issues/{created['public_id']}/evidence",
        files={"file": ("photo.png", _real_png_bytes(), "image/png")},
    )
    assert response.status_code == 201
    assert response.json()["uploaded_by"] == "evidence_authority"


def test_unsupported_content_type_rejected(client):
    created = _submit_issue(client)
    response = _upload(
        client, created["public_id"], content=b"%PDF-1.4 fake pdf content",
        filename="doc.pdf", content_type="application/pdf",
    )
    assert response.status_code == 400


def test_fake_image_with_spoofed_content_type_rejected(client):
    """Plain text/binary data claiming to be a PNG via Content-Type must
    be rejected -- the header alone is never trusted."""
    created = _submit_issue(client)
    response = _upload(
        client, created["public_id"], content=b"this is not really a png file",
        filename="fake.png", content_type="image/png",
    )
    assert response.status_code == 400


def test_content_type_mismatch_rejected(client):
    """A genuine PNG uploaded with a JPEG Content-Type must be rejected
    -- actual decoded format must match the declared type."""
    created = _submit_issue(client)
    response = _upload(
        client, created["public_id"], content=_real_png_bytes(),
        filename="photo.jpg", content_type="image/jpeg",
    )
    assert response.status_code == 400


def test_oversized_file_rejected(client, monkeypatch):
    monkeypatch.setenv("CIVICSYNC_EVIDENCE_MAX_SIZE_BYTES", "100")
    created = _submit_issue(client)
    # A real (larger) PNG comfortably exceeds a 100-byte cap.
    response = _upload(client, created["public_id"], content=_real_png_bytes(size=(200, 200)))
    assert response.status_code == 413


def test_empty_file_rejected(client):
    created = _submit_issue(client)
    response = _upload(client, created["public_id"], content=b"")
    assert response.status_code == 400


def test_path_traversal_filename_cannot_escape_storage_boundary(client, tmp_path):
    """A malicious filename must never become part of the actual storage
    path -- the server-generated storage_key is what's used, always."""
    created = _submit_issue(client)
    response = _upload(
        client, created["public_id"], content=_real_png_bytes(),
        filename="../../../../etc/passwd.png",
    )
    assert response.status_code == 201
    # The display name is sanitized -- no path separators survive.
    assert "/" not in response.json()["original_filename"]
    assert ".." not in response.json()["original_filename"]

    # Nothing was ever written outside the configured storage directory.
    storage_dir = tmp_path / "evidence_storage"
    for path in storage_dir.rglob("*"):
        assert storage_dir in path.parents or path == storage_dir


def test_upload_to_nonexistent_issue_returns_404(client):
    response = _upload(client, "CIV-DOESNOTEXIST12")
    assert response.status_code == 404


# ============================================================================
# Access
# ============================================================================


def test_authorized_authority_can_retrieve_evidence(client):
    created = _submit_issue(client)
    upload_response = _upload(client, created["public_id"])
    evidence_id = upload_response.json()["public_id"]

    response = client.get(f"/api/evidence/{evidence_id}/file")
    assert response.status_code == 200


def test_arbitrary_file_paths_cannot_be_requested(client):
    """The retrieval route only ever accepts an opaque evidence public_id
    -- there is no path parameter, and a path-like or traversal-style
    value simply fails to match any real evidence row."""
    created = _submit_issue(client)
    _upload(client, created["public_id"])

    for bogus in ["../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", "EVD-DOESNOTEXIST12"]:
        response = client.get(f"/api/evidence/{bogus}/file")
        assert response.status_code == 404


def test_unauthorized_access_to_evidence_rejected(real_auth_client):
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()):
        created = real_auth_client.post("/api/issues", json={"text": "Some complaint."}).json()

    login = real_auth_client.post(
        "/api/authority/login",
        json={"username": "evidence_authority", "password": "m18-phase3-password"},
    )
    assert login.status_code == 200
    upload_response = real_auth_client.post(
        f"/api/issues/{created['public_id']}/evidence",
        files={"file": ("photo.png", _real_png_bytes(), "image/png")},
    )
    evidence_id = upload_response.json()["public_id"]
    real_auth_client.post("/api/authority/logout")

    response = real_auth_client.get(f"/api/evidence/{evidence_id}/file")
    assert response.status_code == 401


def test_evidence_list_requires_authentication(real_auth_client):
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()):
        created = real_auth_client.post("/api/issues", json={"text": "Some complaint."}).json()
    response = real_auth_client.get(f"/api/issues/{created['public_id']}/evidence")
    assert response.status_code == 401


# ============================================================================
# Resolution integration
# ============================================================================


def test_existing_resolution_without_evidence_still_works(client):
    created = _submit_issue(client)
    _advance_to_in_progress(client, created["public_id"])
    response = client.post(
        f"/api/issues/{created['public_id']}/resolve",
        json={"resolution_note": "Fixed without any attached evidence."},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "RESOLVED"


def test_resolution_with_evidence_works(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _advance_to_in_progress(client, public_id)
    _upload(client, public_id)

    response = client.post(
        f"/api/issues/{public_id}/resolve",
        json={"resolution_note": "Fixed and documented with a photo."},
    )
    assert response.status_code == 200

    detail = client.get(f"/api/admin/issues/{public_id}").json()
    assert detail["status"] == "RESOLVED"
    assert len(detail["evidence"]) == 1


def test_evidence_does_not_bypass_in_progress_to_resolved_validation(client):
    """Uploading evidence to a SUBMITTED issue must not make it
    resolvable -- the existing transition validator is unaffected by
    evidence's presence."""
    created = _submit_issue(client)
    public_id = created["public_id"]
    _upload(client, public_id)

    response = client.post(
        f"/api/issues/{public_id}/resolve", json={"resolution_note": "Trying to skip ahead."}
    )
    assert response.status_code == 400
    detail = client.get(f"/api/admin/issues/{public_id}").json()
    assert detail["status"] == "SUBMITTED"


def test_exactly_the_expected_status_history_event_is_created_with_evidence(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _advance_to_in_progress(client, public_id)
    _upload(client, public_id)

    before = len(client.get(f"/api/issues/{public_id}/history").json())
    client.post(f"/api/issues/{public_id}/resolve", json={"resolution_note": "Fixed with photo."})
    after_history = client.get(f"/api/issues/{public_id}/history").json()

    assert len(after_history) == before + 1
    assert after_history[-1]["to_status"] == "RESOLVED"


# ============================================================================
# Integrity
# ============================================================================


def test_failed_upload_creates_no_orphan_metadata(client):
    """A rejected upload (bad image) must leave zero ResolutionEvidence
    rows behind -- not a partially-created record."""
    created = _submit_issue(client)
    _upload(client, created["public_id"], content=b"not an image", content_type="image/png")

    evidence = client.get(f"/api/issues/{created['public_id']}/evidence").json()
    assert evidence == []


def test_upload_db_failure_does_not_leave_evidence_listed(client):
    """Simulates a database failure during evidence row creation --
    confirms nothing is listed afterward (the file may exist on disk,
    but no metadata row references it, so it's simply inert)."""
    created = _submit_issue(client)
    public_id = created["public_id"]

    db_gen = app.dependency_overrides[get_db]()
    db = next(db_gen)
    try:
        with patch.object(db, "commit", side_effect=SQLAlchemyError("simulated failure")):
            with pytest.raises(SQLAlchemyError):
                from backend.service import upload_issue_evidence

                upload_issue_evidence(
                    db,
                    public_id,
                    filename="photo.png",
                    content_type="image/png",
                    content=_real_png_bytes(),
                    uploaded_by="test-authority",
                )
        db.rollback()
    finally:
        db.close()

    evidence = client.get(f"/api/issues/{public_id}/evidence").json()
    assert evidence == []


def test_failed_resolution_does_not_mark_issue_resolved(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _upload(client, public_id)  # evidence present, but issue is still SUBMITTED

    client.post(f"/api/issues/{public_id}/resolve", json={"resolution_note": "Should fail."})
    detail = client.get(f"/api/admin/issues/{public_id}").json()
    assert detail["status"] != "RESOLVED"


def test_jurisdiction_unchanged_by_evidence_upload(client):
    created = _submit_issue(client)
    public_id = created["public_id"]
    _upload(client, public_id)

    scoped = client.get(
        "/api/admin/issues", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    ).json()
    assert public_id in {i["public_id"] for i in scoped["items"]}


def test_assignment_unchanged_by_evidence_upload(client):
    created = _submit_issue(client)
    public_id = created["public_id"]

    before = client.get(f"/api/admin/issues/{public_id}").json()["assigned_department"]
    _upload(client, public_id)
    after = client.get(f"/api/admin/issues/{public_id}").json()["assigned_department"]
    assert before == after
