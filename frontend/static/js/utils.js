/**
 * Shared formatting helpers. Nothing here calls the API or touches the
 * DOM directly beyond returning strings/classes -- pure presentation
 * logic reused by report.js, track.js, and admin.js.
 */

const CivicSyncUtils = {
  /** severity value (e.g. "High") -> chip CSS class */
  severityChipClass(severity) {
    const key = (severity || 'unknown').toLowerCase();
    const known = ['low', 'medium', 'high', 'critical'];
    return known.includes(key) ? `chip-severity-${key}` : 'chip-severity-unknown';
  },

  /** IssueStatus value (e.g. "IN_PROGRESS") -> chip CSS class */
  statusChipClass(status) {
    const terminalSuccess = new Set(['RESOLVED', 'CLOSED']);
    const terminalNegative = new Set(['REJECTED']);
    const inProgress = new Set(['ROUTED', 'ACKNOWLEDGED', 'IN_PROGRESS', 'REOPENED']);
    if (terminalSuccess.has(status)) return 'chip-success';
    if (terminalNegative.has(status)) return 'chip-error';
    if (inProgress.has(status)) return 'chip-progress';
    return 'chip-neutral'; // SUBMITTED, CLASSIFIED
  },

  /**
   * CivicSync targets an India-based civic authority/citizen audience, so
   * every timestamp in the product is shown in Asia/Kolkata (IST) --
   * explicitly, via Intl.DateTimeFormat's `timeZone` option, regardless of
   * the viewer's own device/browser timezone. This is deliberate: two
   * different officials looking at the same report should see the same
   * displayed time.
   *
   * IMPORTANT: this only produces a correct result if `isoString` is
   * unambiguous (carries a "Z" or "+HH:MM" offset). The backend
   * (backend/schemas.py's UtcDatetime type) guarantees every timestamp it
   * returns does. Do NOT remove that guarantee and rely on this function
   * alone -- a timezone-designator-less string is parsed by `new Date()`
   * as LOCAL time per the ECMA-262 spec, which silently reintroduces the
   * exact bug this constant exists to prevent.
   */
  DISPLAY_TIME_ZONE: 'Asia/Kolkata',

  /** ISO timestamp -> short, IST, human-readable string. */
  formatTimestamp(isoString) {
    if (!isoString) return '\u2014';
    try {
      const date = new Date(isoString);
      if (Number.isNaN(date.getTime())) return isoString;
      return new Intl.DateTimeFormat('en-IN', {
        timeZone: CivicSyncUtils.DISPLAY_TIME_ZONE,
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
        hour12: true,
      }).format(date) + ' IST';
    } catch (err) {
      return isoString;
    }
  },

  /** InsightPriority value (e.g. "high") -> chip CSS class */
  insightPriorityChipClass(priority) {
    const key = (priority || '').toLowerCase();
    if (key === 'high') return 'chip-error';
    if (key === 'medium') return 'chip-warning';
    return 'chip-neutral'; // low
  },

  /** IssueStatus value (e.g. "IN_PROGRESS") -> "In Progress". Mirrors
   * backend/service.py's _status_label() exactly, for the few places the
   * API doesn't already supply status_label (history rows, lifecycle
   * action buttons). Prefer the API's own status_label field when it's
   * present in the response. */
  statusLabel(status) {
    if (!status) return '';
    return status
      .split('_')
      .map((word) => word.charAt(0) + word.slice(1).toLowerCase())
      .join(' ');
  },

  /** Basic client-side shape check, mirrors backend PUBLIC_ID_PATTERN
   * (CIV- + 12 uppercase hex chars). Purely for fast UX feedback --
   * the backend's own validation is always the source of truth. */
  looksLikePublicId(value) {
    return /^CIV-[0-9A-F]{12}$/.test((value || '').trim());
  },

  escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = value == null ? '' : String(value);
    return div.innerHTML;
  },
};

window.CivicSyncUtils = CivicSyncUtils;
