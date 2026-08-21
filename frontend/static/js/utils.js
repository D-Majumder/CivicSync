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

  /** ISO timestamp -> short, locale-aware, human-readable string */
  formatTimestamp(isoString) {
    if (!isoString) return '\u2014';
    try {
      const date = new Date(isoString);
      if (Number.isNaN(date.getTime())) return isoString;
      return date.toLocaleString(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      });
    } catch (err) {
      return isoString;
    }
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
