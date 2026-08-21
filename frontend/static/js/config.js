/**
 * CivicSync frontend configuration.
 *
 * The frontend is served by the same FastAPI app as the API (see
 * backend/main.py's static mount + page routes), so the default is an
 * empty base URL -- every request is same-origin/relative. This file
 * exists so the backend URL is configured in exactly one place rather
 * than hardcoded as "http://localhost:8000" throughout the app: if the
 * frontend is ever served separately from the API, only this one value
 * needs to change.
 */
window.CIVICSYNC_CONFIG = {
  apiBaseUrl: '',
};
