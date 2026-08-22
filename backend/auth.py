"""
Authority authentication for CivicSync (Milestone 16-A).

Deliberately minimal: ONE demo authority account, configured via
environment variables (matching the existing GEMINI_API_KEY convention
in ai/client.py), with a signed HttpOnly session cookie. No user table,
no migration, no RBAC -- exactly the scope this milestone calls for.

Password storage: the demo credential's PASSWORD HASH (not the plaintext
password) is what belongs in .env -- see AUTHORITY_PASSWORD_HASH below.
hash_password() is provided so a real hash can be generated once, e.g.:

    python3 -c "from backend.auth import hash_password; print(hash_password('your-password'))"

Session mechanism: a signed, timestamped, stateless token (itsdangerous,
already a transitive FastAPI/Starlette dependency -- no new package)
stored in an HttpOnly cookie. Stateless by design: no server-side session
table, so logout works by simply clearing the cookie and login works
without touching the database at all.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SESSION_COOKIE_NAME = "csync_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 8  # 8 hours

_PBKDF2_ITERATIONS = 260_000


def _get_session_secret() -> str:
    """The signing secret for session cookies. Must never be exposed to
    frontend JavaScript or logged. Falls back to a per-process random
    secret if SESSION_SECRET isn't set, so a missing env var fails safe
    (every existing session becomes invalid on restart, rather than the
    app running with a predictable/empty secret)."""
    secret = os.environ.get("SESSION_SECRET")
    if secret:
        return secret
    return _RANDOM_FALLBACK_SECRET


_RANDOM_FALLBACK_SECRET = secrets.token_hex(32)


def hash_password(password: str, salt: str | None = None) -> str:
    """Hash a password with PBKDF2-HMAC-SHA256 (stdlib only, no new
    dependency). Returns "salt$hash" as a single hex-encoded string,
    suitable for pasting into AUTHORITY_PASSWORD_HASH in .env."""
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ITERATIONS
    )
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time comparison against a "salt$hash" string produced by
    hash_password(). Returns False (never raises) for a malformed stored
    hash, so a misconfigured .env fails closed rather than erroring."""
    try:
        salt, _ = stored_hash.split("$", 1)
    except ValueError:
        return False
    candidate = hash_password(password, salt=salt)
    return hmac.compare_digest(candidate, stored_hash)


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_get_session_secret(), salt="civicsync-authority-session")


def create_session_token(username: str) -> str:
    """A signed, timestamped token identifying an authenticated authority
    session. Stateless -- verifying it requires no database or in-memory
    session store, so it survives an app restart (as long as
    SESSION_SECRET is configured) and "logout" is simply deleting the
    cookie client-side (see the /authority/logout route)."""
    return _serializer().dumps({"username": username})


def verify_session_token(token: str | None) -> str | None:
    """Verify a session token's signature and expiry. Returns the
    authenticated username, or None if the token is missing, invalid,
    tampered with, or expired."""
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    return data.get("username")


def authenticate(username: str, password: str) -> bool:
    """Check a username/password pair against the single configured demo
    authority account. Both AUTHORITY_USERNAME and
    AUTHORITY_PASSWORD_HASH must be set -- if either is missing, this
    always returns False (fails closed) rather than allowing an
    unconfigured deployment to silently accept any credential."""
    expected_username = os.environ.get("AUTHORITY_USERNAME")
    expected_hash = os.environ.get("AUTHORITY_PASSWORD_HASH")
    if not expected_username or not expected_hash:
        return False
    if not hmac.compare_digest(username, expected_username):
        return False
    return verify_password(password, expected_hash)


def get_current_authority(request: Request) -> str:
    """FastAPI dependency: require a valid authority session. Raises 401
    (never a redirect -- this is for API routes, which the frontend's
    api.js handles by redirecting to /authority/login itself) if the
    session cookie is missing, invalid, tampered with, or expired.

    This is the ONLY thing that gates every /api/admin/* route (and the
    assignment/status mutation routes) -- protection lives entirely
    server-side, per this milestone's explicit requirement, not in
    frontend hiding.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    username = verify_session_token(token)
    if username is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authority login required.",
        )
    return username


def is_secure_cookie_environment() -> bool:
    """Whether session cookies should be marked Secure (HTTPS-only).
    Mirrors the existing APP_ENV convention rather than inventing a new
    one -- Secure is enabled whenever APP_ENV is anything other than the
    local-development default, so the hackathon's own local run (no
    APP_ENV, or APP_ENV=development) still works over plain http on
    127.0.0.1."""
    return os.environ.get("APP_ENV", "development").lower() not in ("development", "dev", "local")
