"""
Milestone 22: Multilingual Citizen Experience.

Backend/API tests only -- browser-only i18n UI behavior (language
switcher clicks, DOM text swapping) cannot be exercised in a backend
test and is instead verified via `node --check` on the changed JS files
plus a manual browser check (see the M22 report).

Reuses the established single-jurisdiction fixture pattern. Gemini is
mocked throughout -- no real Gemini calls, so these tests exercise the
REQUEST/RESPONSE and persistence layer, not actual Gemini multilingual
comprehension (which cannot be deterministically tested without a real
API call).
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from ai.schemas import CivicIssue, IssueCategory, SeverityLevel
from backend.auth import get_current_authority
from backend.database import Base, build_engine, get_db
from backend.main import app
from backend.models import Issue, Jurisdiction, JurisdictionLevel


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
    db_path = tmp_path / "test_m22.db"
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
    test_client._Session = TestingSessionLocal
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _submit(client, text, **body):
    payload = {"text": text}
    payload.update(body)
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue()) as mock_analyze:
        response = client.post("/api/issues", json=payload)
    return response, mock_analyze


# ============================================================================
# English / Hindi / Bengali complaint submission
# ============================================================================


def test_english_complaint_submission(client):
    response, _ = _submit(client, "There is a large pothole on the main road.", language="en")
    assert response.status_code == 201
    assert response.json()["citizen_language"] == "en"


def test_hindi_complaint_submission(client):
    text = "हमारे स्कूल के पास दो हफ्तों से स्ट्रीट लाइट खराब है।"
    response, mock_analyze = _submit(client, text, language="hi")
    assert response.status_code == 201
    assert response.json()["citizen_language"] == "hi"
    mock_analyze.assert_called_once_with(text)


def test_bengali_complaint_submission(client):
    text = "আমাদের স্কুলের কাছে দুই সপ্তাহ ধরে রাস্তার বাতি নষ্ট।"
    response, mock_analyze = _submit(client, text, language="bn")
    assert response.status_code == 201
    assert response.json()["citizen_language"] == "bn"
    mock_analyze.assert_called_once_with(text)


def test_invalid_language_code_rejected(client):
    response, _ = _submit(client, "Some complaint.", language="fr")
    assert response.status_code == 422


def test_language_optional_defaults_to_null(client):
    response, _ = _submit(client, "Some complaint.")
    assert response.status_code == 201
    assert response.json()["citizen_language"] is None


# ============================================================================
# Original text preservation
# ============================================================================


def test_original_text_preserved_exactly_hindi(client):
    text = "गली में  कचरा  पड़ा है, सफाई नहीं हुई।"
    payload = {"text": text, "language": "hi"}
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue(original_text=text)):
        response = client.post("/api/issues", json=payload)
    public_id = response.json()["public_id"]

    db = client._Session()
    issue = db.query(Issue).filter(Issue.public_id == public_id).one()
    assert issue.original_text == text
    db.close()


def test_original_text_preserved_exactly_bengali(client):
    text = "রাস্তায় জল জমে আছে, তিন দিন ধরে।"
    payload = {"text": text, "language": "bn"}
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue(original_text=text)):
        response = client.post("/api/issues", json=payload)
    public_id = response.json()["public_id"]

    db = client._Session()
    issue = db.query(Issue).filter(Issue.public_id == public_id).one()
    assert issue.original_text == text
    db.close()


def test_ai_never_overwrites_original_text(client):
    """The AI's structured `problem` field is separate from
    original_text -- even when Gemini's returned CivicIssue has
    different wording, original_text must remain exactly what the
    citizen typed."""
    text = "कचरा जमा है सड़क पर।"
    civic_issue = _sample_civic_issue(original_text=text, problem="Garbage accumulation on the road.")
    with patch("backend.service.analyze_complaint", return_value=civic_issue):
        response = client.post("/api/issues", json={"text": text, "language": "hi"})
    public_id = response.json()["public_id"]

    db = client._Session()
    issue = db.query(Issue).filter(Issue.public_id == public_id).one()
    assert issue.original_text == text
    assert issue.problem == "Garbage accumulation on the road."
    db.close()


# ============================================================================
# Multilingual AI pipeline invocation / tolerance behavior
# ============================================================================


def test_multilingual_pipeline_invoked_for_each_language(client):
    """Confirms analyze_complaint (the single, shared extraction
    pipeline) is called for every language -- no separate/duplicated AI
    system per language."""
    for text, lang in [
        ("Pothole on Main Street.", "en"),
        ("सड़क पर गड्ढा है।", "hi"),
        ("রাস্তায় গর্ত আছে।", "bn"),
    ]:
        _, mock_analyze = _submit(client, text, language=lang)
        mock_analyze.assert_called_once_with(text)


def test_spelling_error_tolerance_does_not_alter_submission_flow(client):
    """A misspelled/colloquial complaint must submit and persist
    identically to a clean one -- tolerance is a Gemini prompt
    instruction (ai/prompts.py), not special-cased backend logic."""
    text = "strret lite not workin near skool 4 two weeks"
    response, mock_analyze = _submit(client, text)
    assert response.status_code == 201
    mock_analyze.assert_called_once_with(text)


def test_incomplete_word_input_still_submits(client):
    text = "potho... near mkt very dang"
    response, _ = _submit(client, text)
    assert response.status_code == 201


def test_ambiguous_input_does_not_receive_fabricated_facts(client):
    """When Gemini (mocked here to simulate a genuinely ambiguous
    response) can't confidently extract details, the resulting Issue
    must reflect that honestly -- unknown/null fields and low
    confidence, never invented specifics. This tests that the backend
    faithfully persists whatever Gemini actually returns, without
    padding in missing fields itself."""
    vague_response = CivicIssue(
        original_text="there's a problem somewhere",
        category=IssueCategory.OTHER,
        problem="Unspecified civic issue.",
        location=None,
        duration=None,
        affected_population=None,
        severity=SeverityLevel.UNKNOWN,
        confidence=0.2,
    )
    with patch("backend.service.analyze_complaint", return_value=vague_response):
        response = client.post("/api/issues", json={"text": "there's a problem somewhere"})
    assert response.status_code == 201
    body = response.json()
    assert body["category"] == "Other"
    assert body["location"] is None
    assert body["duration"] is None
    assert body["affected_population"] is None
    assert body["severity"] == "Unknown"
    assert body["confidence"] == 0.2


# ============================================================================
# Admin remains functional / receives structured English fields
# ============================================================================


def test_admin_receives_structured_english_fields_regardless_of_input_language(client):
    text = "सड़क पर गड्ढा है।"
    civic_issue = _sample_civic_issue(category=IssueCategory.ROADS_AND_POTHOLES, severity=SeverityLevel.HIGH)
    with patch("backend.service.analyze_complaint", return_value=civic_issue):
        response = client.post("/api/issues", json={"text": text, "language": "hi"})
    public_id = response.json()["public_id"]

    detail = client.get(f"/api/admin/issues/{public_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["category"] == "Roads and Potholes"  # English enum value
    assert body["severity"] == "High"  # English enum value


def test_admin_can_see_original_citizen_description(client):
    """Milestone 22 requirement: authority can audit what was actually
    submitted -- original_text must be visible in the admin detail
    view."""
    text = "आमার बাড়ির কাছে রাস্তার আলো নষ্ট।"
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue(original_text=text)):
        response = client.post("/api/issues", json={"text": text, "language": "bn"})
    public_id = response.json()["public_id"]

    detail = client.get(f"/api/admin/issues/{public_id}")
    assert detail.json()["original_text"] == text


def test_admin_issue_list_still_works(client):
    _submit(client, "Some complaint.", language="hi")
    response = client.get(
        "/api/admin/issues", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    )
    assert response.status_code == 200


def test_admin_dashboard_still_works(client):
    _submit(client, "Some complaint.")
    response = client.get(
        "/api/admin/dashboard/summary", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    )
    assert response.status_code == 200


# ============================================================================
# Tracking works
# ============================================================================


def test_tracking_works_for_multilingual_submission(client):
    text = "রাস্তায় গর্ত আছে, খুব বিপজ্জনক।"
    response, _ = _submit(client, text, language="bn")
    public_id = response.json()["public_id"]

    tracked = client.get(f"/api/track/{public_id}")
    assert tracked.status_code == 200
    assert tracked.json()["status"] == "SUBMITTED"


def test_public_tracking_omits_citizen_language(client):
    """citizen_language is an authority-audit field -- not exposed on
    the public tracking response (keeps the existing public/security
    boundary unchanged)."""
    response, _ = _submit(client, "Some complaint.", language="hi")
    public_id = response.json()["public_id"]
    tracked = client.get(f"/api/track/{public_id}")
    assert "citizen_language" not in tracked.json()


def test_resolution_and_reopening_flow_works_regardless_of_submission_language(client):
    text = "सड़क पर पानी भरा है।"
    response, _ = _submit(client, text, language="hi")
    public_id = response.json()["public_id"]

    for status_value in ["CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS"]:
        assert client.post(f"/api/issues/{public_id}/status", json={"status": status_value}).status_code == 200
    resolve = client.post(f"/api/issues/{public_id}/resolve", json={"resolution_note": "Fixed."})
    assert resolve.status_code == 200

    reopen = client.post(
        f"/api/track/{public_id}/reopen-request", json={"reason": "Problem returned."}
    )
    assert reopen.status_code == 201

    tracked = client.get(f"/api/track/{public_id}")
    assert tracked.json()["resolution_summary"] == "Fixed."
    assert tracked.json()["active_reopen_request"]["state"] == "PENDING"


def test_evidence_flow_works_regardless_of_submission_language(client):
    from io import BytesIO
    from PIL import Image

    text = "রাস্তায় গর্ত।"
    response, _ = _submit(client, text, language="bn")
    public_id = response.json()["public_id"]
    for status_value in ["CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS"]:
        client.post(f"/api/issues/{public_id}/status", json={"status": status_value})

    buf = BytesIO()
    Image.new("RGB", (10, 10), color="red").save(buf, format="PNG")
    upload = client.post(
        f"/api/issues/{public_id}/evidence", files={"file": ("photo.png", buf.getvalue(), "image/png")}
    )
    assert upload.status_code == 201


# ============================================================================
# Regression: English behavior, lifecycle, jurisdiction, coordinates, evidence
# ============================================================================


def test_regression_english_submission_unaffected(client):
    response, _ = _submit(client, "There has been no street light near our school for two weeks.")
    assert response.status_code == 201
    body = response.json()
    assert body["category"] == "Roads and Potholes"
    assert body["status"] == "SUBMITTED"


def test_regression_no_lifecycle_change(client):
    response, _ = _submit(client, "Some complaint.", language="hi")
    assert response.json()["status"] == "SUBMITTED"


def test_regression_jurisdiction_resolution_unaffected_by_language(client):
    response, _ = _submit(client, "Some complaint.", language="bn")
    public_id = response.json()["public_id"]
    scoped = client.get(
        "/api/admin/issues", params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"}
    )
    ids = {i["public_id"] for i in scoped.json()["items"]}
    assert public_id in ids


def test_regression_m21_coordinates_still_work_alongside_language(client):
    response, _ = _submit(
        client, "सड़क पर गड्ढा है।", language="hi", latitude=23.4058, longitude=88.4894, accuracy=10
    )
    assert response.status_code == 201
    body = response.json()
    assert body["latitude"] == 23.4058
    assert body["citizen_language"] == "hi"


def test_regression_coordinate_validation_unaffected_by_language_field(client):
    response, _ = _submit(client, "Some complaint.", language="hi", latitude=999, longitude=88.4894)
    assert response.status_code == 422


def test_regression_no_extra_history_record_from_language(client):
    response, _ = _submit(client, "Some complaint.", language="en")
    public_id = response.json()["public_id"]
    history = client.get(f"/api/issues/{public_id}/history")
    assert len(history.json()) == 1
