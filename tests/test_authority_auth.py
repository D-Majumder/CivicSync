"""
Milestone 16-A: Authority Portal authentication.

Unlike most other API test files, these tests do NOT override
get_current_authority -- the whole point is to exercise the REAL
authentication flow (real login, real session cookie, real 401s) against
an isolated database. AUTHORITY_USERNAME/AUTHORITY_PASSWORD_HASH/
SESSION_SECRET are set via monkeypatched environment variables so no real
.env is required to run these tests.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from ai.schemas import CivicIssue, IssueCategory, SeverityLevel
from backend.auth import SESSION_COOKIE_NAME, hash_password
from backend.database import Base, build_engine, get_db
from backend.main import app
from backend.models import Department

TEST_USERNAME = "test_authority"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_PASSWORD_HASH = hash_password(TEST_PASSWORD)


def _sample_civic_issue(**overrides) -> CivicIssue:
    base = dict(
        original_text="Sample complaint.",
        category=IssueCategory.OTHER,
        problem="Sample problem.",
        severity=SeverityLevel.MEDIUM,
        confidence=0.75,
    )
    base.update(overrides)
    return CivicIssue(**base)


@pytest.fixture
def auth_env(monkeypatch):
    """Real demo credentials + session secret, via env vars -- exactly
    how the real app is configured, never touching a real .env file."""
    monkeypatch.setenv("AUTHORITY_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("AUTHORITY_PASSWORD_HASH", TEST_PASSWORD_HASH)
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-for-authority-auth-tests")
    yield


@pytest.fixture
def client(auth_env, tmp_path):
    """An isolated test database + a REAL (not mocked) authentication
    dependency -- app.dependency_overrides intentionally does NOT include
    get_current_authority here."""
    db_path = tmp_path / "test_authority_auth.db"
    engine = build_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    seed_db = TestingSessionLocal()
    seed_db.add(Department(code="STREET_LIGHTING", name="Street Lighting", is_active=True))
    seed_db.commit()
    seed_db.close()

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


def _submit_issue(client, **overrides) -> dict:
    with patch("backend.service.analyze_complaint", return_value=_sample_civic_issue(**overrides)):
        response = client.post("/api/issues", json={"text": "Some complaint."})
    assert response.status_code == 201
    return response.json()


# --- Successful login -----------------------------------------------------------


def test_login_with_correct_credentials_succeeds(client):
    response = client.post(
        "/api/authority/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200
    assert response.json() == {"success": True}
    assert SESSION_COOKIE_NAME in response.cookies


def test_login_sets_httponly_cookie(client):
    response = client.post(
        "/api/authority/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
    )
    set_cookie_header = response.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie_header


# --- Invalid credentials ----------------------------------------------------------


def test_login_with_wrong_password_rejected(client):
    response = client.post(
        "/api/authority/login", json={"username": TEST_USERNAME, "password": "wrong-password"}
    )
    assert response.status_code == 401
    assert SESSION_COOKIE_NAME not in response.cookies


def test_login_with_wrong_username_rejected(client):
    response = client.post(
        "/api/authority/login", json={"username": "not-the-real-user", "password": TEST_PASSWORD}
    )
    assert response.status_code == 401


def test_login_error_message_does_not_reveal_which_field_was_wrong(client):
    response_bad_user = client.post(
        "/api/authority/login", json={"username": "nope", "password": TEST_PASSWORD}
    )
    response_bad_pass = client.post(
        "/api/authority/login", json={"username": TEST_USERNAME, "password": "nope"}
    )
    assert response_bad_user.json()["detail"] == response_bad_pass.json()["detail"]


# --- Unauthenticated /admin ---------------------------------------------------------


def test_unauthenticated_admin_page_redirects_to_login(client):
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/authority/login"


def test_authenticated_admin_page_serves_the_real_page(client):
    client.post("/api/authority/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 200
    assert "Command Center" in response.text or "CivicSync" in response.text


def test_login_page_redirects_to_admin_if_already_authenticated(client):
    client.post("/api/authority/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    response = client.get("/authority/login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/admin"


# --- Unauthenticated authority API returns 401 -------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/admin/dashboard/summary"),
        ("get", "/api/admin/issues"),
        ("get", "/api/admin/queue"),
        ("get", "/api/admin/issues/stale"),
        ("get", "/api/admin/insights"),
        ("post", "/api/admin/insights/prioritize"),
        ("get", "/api/admin/departments/STREET_LIGHTING/summary"),
        ("get", "/api/admin/analytics/funnel"),
        ("get", "/api/admin/analytics/severity-distribution"),
        ("get", "/api/admin/analytics/department-workload"),
        ("get", "/api/admin/analytics/aging"),
        ("get", "/api/admin/analytics/resolution-timing"),
        ("get", "/api/admin/analytics/recent-activity"),
    ],
)
def test_unauthenticated_admin_api_returns_401(client, method, path):
    response = getattr(client, method)(path)
    assert response.status_code == 401
    assert "Traceback" not in response.text


def test_unauthenticated_operational_briefing_returns_401(client):
    response = client.post(
        "/api/admin/analytics/operational-briefing",
        params={"jurisdiction_code": "IN-WB-NADIA-KRISHNANAGAR"},
    )
    assert response.status_code == 401


def test_unauthenticated_explain_returns_401(client):
    created = _submit_issue(client)
    response = client.post(f"/api/admin/issues/{created['public_id']}/explain")
    assert response.status_code == 401


def test_unauthenticated_assignment_and_status_return_401(client):
    created = _submit_issue(client)
    r1 = client.post(
        f"/api/issues/{created['public_id']}/assignment",
        json={"department_code": "STREET_LIGHTING"},
    )
    r2 = client.post(f"/api/issues/{created['public_id']}/status", json={"status": "CLASSIFIED"})
    r3 = client.get(f"/api/issues/{created['public_id']}/assignment")
    assert r1.status_code == 401
    assert r2.status_code == 401
    assert r3.status_code == 401


def test_unauthenticated_full_issue_detail_returns_401(client):
    """GET /api/issues/{public_id} (distinct from the public
    /api/track/{public_id}) exposes internal/authority-only fields and
    must require authentication too."""
    created = _submit_issue(client)
    response = client.get(f"/api/issues/{created['public_id']}")
    assert response.status_code == 401


# --- Authenticated authority API works ----------------------------------------------


def test_authenticated_admin_api_works(client):
    client.post("/api/authority/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    response = client.get("/api/admin/dashboard/summary")
    assert response.status_code == 200


def test_authenticated_full_issue_detail_works(client):
    client.post("/api/authority/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    created = _submit_issue(client)
    response = client.get(f"/api/issues/{created['public_id']}")
    assert response.status_code == 200


# --- Logout invalidates the session --------------------------------------------------


def test_logout_clears_the_session(client):
    client.post("/api/authority/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    assert client.get("/api/admin/dashboard/summary").status_code == 200

    logout_response = client.post("/api/authority/logout")
    assert logout_response.status_code == 200

    assert client.get("/api/admin/dashboard/summary").status_code == 401
    assert client.get("/admin", follow_redirects=False).status_code == 302


def test_logout_always_succeeds_even_when_not_logged_in(client):
    response = client.post("/api/authority/logout")
    assert response.status_code == 200


# --- Citizen/public endpoints remain accessible without login -----------------------


def test_public_issue_submission_requires_no_login(client):
    created = _submit_issue(client)
    assert created["status"] == "SUBMITTED"


def test_public_tracking_requires_no_login(client):
    created = _submit_issue(client)
    response = client.get(f"/api/track/{created['public_id']}")
    assert response.status_code == 200


def test_public_departments_list_requires_no_login(client):
    assert client.get("/api/departments").status_code == 200


def test_public_jurisdictions_list_requires_no_login(client):
    assert client.get("/api/jurisdictions").status_code == 200


def test_health_requires_no_login(client):
    assert client.get("/health").status_code == 200


def test_citizen_home_page_requires_no_login(client):
    assert client.get("/").status_code == 200


def test_public_track_page_requires_no_login(client):
    assert client.get("/track").status_code == 200


# --- Admin APIs cannot be bypassed by directly calling the endpoint -----------------


def test_tampered_session_cookie_is_rejected(client):
    client.post("/api/authority/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    real_cookie = client.cookies.get(SESSION_COOKIE_NAME)
    client.cookies.set(SESSION_COOKIE_NAME, real_cookie[:-4] + "AAAA")
    response = client.get("/api/admin/dashboard/summary")
    assert response.status_code == 401


def test_forged_cookie_with_wrong_secret_is_rejected(client, monkeypatch):
    """A token signed with a DIFFERENT secret (e.g. crafted by an
    attacker without knowing SESSION_SECRET) must be rejected -- proves
    the signature is actually verified, not merely present."""
    import itsdangerous

    forged = itsdangerous.URLSafeTimedSerializer(
        "attacker-guessed-wrong-secret", salt="civicsync-authority-session"
    ).dumps({"username": TEST_USERNAME})
    client.cookies.set(SESSION_COOKIE_NAME, forged)
    response = client.get("/api/admin/dashboard/summary")
    assert response.status_code == 401


def test_empty_cookie_value_is_rejected(client):
    client.cookies.set(SESSION_COOKIE_NAME, "")
    response = client.get("/api/admin/dashboard/summary")
    assert response.status_code == 401


# --- Credentials/secrets are not exposed in frontend assets --------------------------


def test_login_page_html_contains_no_secrets(client):
    response = client.get("/authority/login")
    text = response.text
    assert TEST_PASSWORD_HASH not in text
    assert "SESSION_SECRET" not in text
    assert "AUTHORITY_PASSWORD_HASH" not in text
    assert "GEMINI_API_KEY" not in text


def test_admin_page_html_contains_no_secrets(client):
    client.post("/api/authority/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    response = client.get("/admin")
    text = response.text
    assert TEST_PASSWORD_HASH not in text
    assert "SESSION_SECRET" not in text
    assert "GEMINI_API_KEY" not in text


def test_admin_js_contains_no_secrets():
    """Static check of the shipped JS asset itself."""
    with open("frontend/static/js/admin.js") as f:
        content = f.read()
    assert "SESSION_SECRET" not in content
    assert "GEMINI_API_KEY" not in content
    assert "AUTHORITY_PASSWORD" not in content
