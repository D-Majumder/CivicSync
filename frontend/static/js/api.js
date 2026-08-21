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

/** Build a query string from a params object, omitting null/undefined/''. */
function toQueryString(params) {
  const usable = Object.entries(params || {}).filter(
    ([, v]) => v !== null && v !== undefined && v !== ''
  );
  if (usable.length === 0) return '';
  return '?' + usable.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join('&');
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

  /** Active department registry. */
  getDepartments() {
    return request('/api/departments', { method: 'GET' });
  },

  /**
   * Jurisdictions in the administrative hierarchy (Country -> State ->
   * District -> Local Body). filters may include level, country_code.
   * Each returned jurisdiction includes its full ancestry chain.
   */
  getJurisdictions(filters) {
    return request(`/api/jurisdictions${toQueryString(filters)}`, { method: 'GET' });
  },

  /** Real aggregate counts (by_status/by_severity/by_department).
   * filters may include jurisdiction_code to scope by_status/by_severity/
   * total_issues to that jurisdiction's subtree. */
  getDashboardSummary(filters) {
    return request(`/api/admin/dashboard/summary${toQueryString(filters)}`, { method: 'GET' });
  },

  // --- Milestone 12: Authority Command Center ---------------------------------

  /**
   * Filtered/sorted/paginated issue list. `filters` may include any of:
   * status, department_code, severity, category, sort_by, sort_order,
   * limit, offset -- all forwarded to GET /api/admin/issues as query
   * params, matching the backend's own filter/sort contract exactly (see
   * backend/main.py's list_admin_issues_route).
   */
  listAdminIssues(filters) {
    return request(`/api/admin/issues${toQueryString(filters)}`, { method: 'GET' });
  },

  /** Stale/aging issues. filters: older_than_hours, department_code, status, limit, offset. */
  getStaleIssues(filters) {
    return request(`/api/admin/issues/stale${toQueryString(filters)}`, { method: 'GET' });
  },

  /** Full authority-facing issue detail, including status/assignment history. */
  getAdminIssueDetail(publicId) {
    return request(`/api/admin/issues/${encodeURIComponent(publicId)}`, { method: 'GET' });
  },

  /** Operational queue (excludes CLOSED/REJECTED). filters: department_code, severity, limit, offset. */
  getOperationalQueue(filters) {
    return request(`/api/admin/queue${toQueryString(filters)}`, { method: 'GET' });
  },

  /** Per-department operational summary (status/severity breakdown, active/resolved/closed). */
  getDepartmentSummary(departmentCode) {
    return request(`/api/admin/departments/${encodeURIComponent(departmentCode)}/summary`, {
      method: 'GET',
    });
  },

  /** Ordered status funnel (same counts as dashboard summary, list-shaped). */
  getFunnel() {
    return request('/api/admin/analytics/funnel', { method: 'GET' });
  },

  /** Severity breakdown with percentages. */
  getSeverityDistribution() {
    return request('/api/admin/analytics/severity-distribution', { method: 'GET' });
  },

  /** Status breakdown for every active department, one call. */
  getDepartmentWorkload() {
    return request('/api/admin/analytics/department-workload', { method: 'GET' });
  },

  /** Age-since-created_at distribution for active issues (neutral buckets, not SLA). */
  getAgingBuckets() {
    return request('/api/admin/analytics/aging', { method: 'GET' });
  },

  /** Most recent status-transition events, system-wide. */
  getRecentActivity(limit) {
    return request(`/api/admin/analytics/recent-activity${toQueryString({ limit })}`, {
      method: 'GET',
    });
  },

  /** Grounded, deterministic civic insights. Never calls Gemini. */
  getInsights() {
    return request('/api/admin/insights', { method: 'GET' });
  },

  /** AI-assisted prioritization over the same grounded insights (advisory only). */
  prioritizeInsights() {
    return request('/api/admin/insights/prioritize', { method: 'POST' });
  },

  /**
   * On-demand AI explanation of a single issue's existing classification
   * (Milestone 14). Advisory/explanatory only -- never changes the issue.
   * Never persisted: a fresh call to Gemini every time, nothing cached
   * server-side.
   */
  explainIssue(publicId) {
    return request(`/api/admin/issues/${encodeURIComponent(publicId)}/explain`, {
      method: 'POST',
    });
  },

  /**
   * On-demand AI operational briefing for a jurisdiction (and optional
   * department) -- Milestone 15. Advisory only -- never changes any
   * issue. Never persisted: a fresh call to Gemini every time.
   */
  generateOperationalBriefing(jurisdictionCode, departmentCode) {
    return request(
      `/api/admin/analytics/operational-briefing${toQueryString({
        jurisdiction_code: jurisdictionCode,
        department_code: departmentCode || null,
      })}`,
      { method: 'POST' }
    );
  },

  /**
   * Officially assign/reassign an issue to a department. This is the
   * ONLY assignment endpoint -- never derived from the AI's
   * suggested_department. May also advance the lifecycle (CLASSIFIED ->
   * ROUTED) as a side effect of the backend's own atomic assignment logic.
   */
  assignDepartment(publicId, departmentCode, reason) {
    return request(`/api/issues/${encodeURIComponent(publicId)}/assignment`, {
      method: 'POST',
      body: JSON.stringify({ department_code: departmentCode, reason: reason || null }),
    });
  },

  /**
   * Transition an issue's status. The backend (backend/transitions.py) is
   * the sole authority on whether a transition is valid -- this call can
   * fail with a 400 even if the frontend believed the transition looked
   * valid.
   */
  transitionStatus(publicId, newStatus, reason) {
    return request(`/api/issues/${encodeURIComponent(publicId)}/status`, {
      method: 'POST',
      body: JSON.stringify({ status: newStatus, reason: reason || null }),
    });
  },
};

window.CivicSyncApi = CivicSyncApi;
