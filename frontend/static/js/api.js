/**
 * CivicSync API client.
 *
 * Every network call the frontend makes goes through here -- no component
 * calls fetch() directly. This keeps the request/response contract in one
 * place and lets every page handle errors consistently.
 *
 * Error taxonomy (see ApiError.type):
 *   'validation'   -> 422, the request itself was malformed
 *   'not_found'    -> 404
 *   'client_error' -> other 4xx (e.g. 400 from an AI parsing failure)
 *   'ai_unavailable' -> 502, the backend's Gemini call failed
 *   'server_error' -> 5xx
 *   'network'      -> fetch() itself failed (offline, DNS, CORS, etc.)
 *
 * Never surfaces raw exception text or Pydantic's internal validation
 * error format -- only the backend's own sanitized `detail` strings (for
 * routes that provide one) or a fixed, citizen-friendly fallback.
 */

class ApiError extends Error {
  constructor(type, message, status = null) {
    super(message);
    this.name = 'ApiError';
    this.type = type;
    this.status = status;
  }
}

function apiBaseUrl() {
  return (window.CIVICSYNC_CONFIG && window.CIVICSYNC_CONFIG.apiBaseUrl) || '';
}

/**
 * The backend's own HTTPException `detail` is always a sanitized,
 * citizen-safe string (see backend/main.py's error handling throughout).
 * FastAPI's automatic 422 validation errors are NOT -- `detail` there is
 * a list of internal Pydantic error objects, which we deliberately never
 * surface. Only use `body.detail` when it's a plain string.
 */
function safeDetail(body, fallback) {
  if (body && typeof body.detail === 'string' && body.detail.trim()) {
    return body.detail;
  }
  return fallback;
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(apiBaseUrl() + path, {
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      ...options,
    });
  } catch (networkError) {
    throw new ApiError(
      'network',
      "We couldn't reach the CivicSync server. Check your connection and try again."
    );
  }

  let body = null;
  try {
    body = await response.json();
  } catch (parseError) {
    body = null;
  }

  if (response.ok) {
    return body;
  }

  if (response.status === 404) {
    throw new ApiError('not_found', safeDetail(body, 'Not found.'), 404);
  }
  if (response.status === 422) {
    throw new ApiError(
      'validation',
      'Please check your entry and try again.',
      422
    );
  }
  if (response.status === 502) {
    throw new ApiError(
      'ai_unavailable',
      'AI analysis is temporarily unavailable. Please try again shortly.',
      502
    );
  }
  if (response.status >= 500) {
    throw new ApiError(
      'server_error',
      'CivicSync is temporarily unavailable. Please try again shortly.',
      response.status
    );
  }
  throw new ApiError(
    'client_error',
    safeDetail(body, 'The request could not be processed.'),
    response.status
  );
}

const CivicSyncApi = {
  ApiError,

  /**
   * Submit a citizen complaint. One backend request: Gemini analysis +
   * persistence happen server-side as a single call -- there is no
   * separate "classify" step the frontend triggers independently.
   * Returns the created Issue (IssueResponse shape).
   */
  submitIssue(text) {
    return request('/api/issues', {
      method: 'POST',
      body: JSON.stringify({ text }),
    });
  },

  /** Public tracking lookup. Returns PublicIssueTrackingResponse. */
  getTracking(publicId) {
    return request(`/api/track/${encodeURIComponent(publicId)}`, { method: 'GET' });
  },

  /** Active department registry, for the citizen-facing trust panel. */
  getDepartments() {
    return request('/api/departments', { method: 'GET' });
  },

  /** Real aggregate counts for the authority placeholder page. */
  getDashboardSummary() {
    return request('/api/admin/dashboard/summary', { method: 'GET' });
  },
};

window.CivicSyncApi = CivicSyncApi;
