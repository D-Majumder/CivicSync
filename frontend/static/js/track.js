/**
 * Public tracking page (track.html). Reads a public_id from the URL path
 * (/track/CIV-XXXXXXXXXXXX) if present, otherwise shows a search box.
 * Renders exactly the fields GET /api/track/{public_id} returns -- no
 * internal ids, nothing fabricated.
 */
(function () {
  const { CivicSyncApi, CivicSyncUtils } = window;

  const searchForm = document.getElementById('track-search-form');
  const searchInput = document.getElementById('track-search-input');

  const loadingState = document.getElementById('track-loading');
  const emptyState = document.getElementById('track-empty');
  const notFoundState = document.getElementById('track-not-found');
  const invalidState = document.getElementById('track-invalid');
  const errorState = document.getElementById('track-error');
  const resultState = document.getElementById('track-result');

  const allStates = [loadingState, emptyState, notFoundState, invalidState, errorState, resultState];

  const elCategory = document.getElementById('track-category');
  const elStatusChip = document.getElementById('track-status-chip');
  const elPublicId = document.getElementById('track-public-id');
  const elProblem = document.getElementById('track-problem');
  const elLocation = document.getElementById('track-location');
  const elDuration = document.getElementById('track-duration');
  const elAffected = document.getElementById('track-affected');
  const elSeverityChip = document.getElementById('track-severity-chip');
  const elDepartment = document.getElementById('track-department');
  const elCreatedAt = document.getElementById('track-created-at');
  const elUpdatedAt = document.getElementById('track-updated-at');
  const elTimeline = document.getElementById('track-timeline');
  const elResolution = document.getElementById('track-resolution');
  const elResolutionNote = document.getElementById('track-resolution-note');
  const elResolvedAt = document.getElementById('track-resolved-at');
  const elEvidence = document.getElementById('track-evidence');
  const elEvidenceList = document.getElementById('track-evidence-list');
  const elReopenSection = document.getElementById('track-reopen-section');
  const elReopenFormWrapper = document.getElementById('track-reopen-form-wrapper');
  const elReopenReason = document.getElementById('track-reopen-reason');
  const elReopenSubmitBtn = document.getElementById('track-reopen-submit-btn');
  const elReopenFeedback = document.getElementById('track-reopen-feedback');
  const elReopenStatus = document.getElementById('track-reopen-status');
  const elReopenStatusText = document.getElementById('track-reopen-status-text');
  const notFoundIdEl = document.getElementById('track-not-found-id');
  const errorMessageEl = document.getElementById('track-error-message');

  function showState(state) {
    allStates.forEach((el) => {
      if (el) el.hidden = el !== state;
    });
  }

  function extractPublicIdFromPath() {
    const match = window.location.pathname.match(/^\/track\/(.+)$/);
    return match ? decodeURIComponent(match[1]) : null;
  }

  function fillOptionalField(el, value) {
    el.textContent = value ? value : window.CivicSyncI18n.t('track_not_specified');
  }

  function renderIssue(issue) {
    elCategory.textContent = issue.category;
    elStatusChip.textContent = window.CivicSyncI18n.statusLabel(issue.status);
    elStatusChip.className = `chip ${CivicSyncUtils.statusChipClass(issue.status)}`;
    elPublicId.textContent = issue.public_id;
    elProblem.textContent = issue.problem;
    fillOptionalField(elLocation, issue.location);
    fillOptionalField(elDuration, issue.duration);
    fillOptionalField(elAffected, issue.affected_population);
    elSeverityChip.textContent = issue.severity;
    elSeverityChip.className = `chip ${CivicSyncUtils.severityChipClass(issue.severity)}`;
    elDepartment.textContent = issue.assigned_department
      ? issue.assigned_department.name
      : window.CivicSyncI18n.t('track_department_pending');
    elCreatedAt.textContent = CivicSyncUtils.formatTimestamp(issue.created_at);
    elUpdatedAt.textContent = CivicSyncUtils.formatTimestamp(issue.updated_at);

    elTimeline.innerHTML = '';
    const timeline = issue.timeline || [];
    timeline.forEach((entry, index) => {
      const isCurrent = index === timeline.length - 1;
      const li = document.createElement('li');
      li.className = `timeline__item${isCurrent ? ' timeline__item--current' : ''}`;
      li.innerHTML = `
        <div class="timeline__status">${CivicSyncUtils.escapeHtml(window.CivicSyncI18n.statusLabel(entry.status))}</div>
        <div class="timeline__timestamp">${CivicSyncUtils.escapeHtml(CivicSyncUtils.formatTimestamp(entry.timestamp))}</div>
        ${entry.reason ? `<div class="timeline__reason">${CivicSyncUtils.escapeHtml(entry.reason)}</div>` : ''}
      `;
      elTimeline.appendChild(li);
    });

    if (issue.resolution_summary) {
      elResolutionNote.textContent = issue.resolution_summary;
      elResolvedAt.textContent = issue.resolved_at
        ? CivicSyncUtils.formatTimestamp(issue.resolved_at)
        : window.CivicSyncI18n.t('track_resolved_prefix').toLowerCase();
      elResolution.hidden = false;
    } else {
      elResolution.hidden = true;
    }

    const evidence = issue.evidence || [];
    if (evidence.length > 0) {
      elEvidenceList.innerHTML = '';
      evidence.forEach((item) => {
        const li = document.createElement('li');
        li.style.marginBottom = '6px';
        const link = document.createElement('a');
        link.href = `/api/track/${encodeURIComponent(issue.public_id)}/evidence/${encodeURIComponent(item.public_id)}/file`;
        link.target = '_blank';
        link.rel = 'noopener';
        link.className = 'evidence-link';
        link.textContent = `\ud83d\udcce ${item.original_filename}`;
        li.appendChild(link);
        const meta = document.createElement('span');
        meta.className = 'type-body-sm';
        meta.style.color = 'var(--color-on-surface-variant)';
        meta.style.marginLeft = '8px';
        meta.textContent = CivicSyncUtils.formatTimestamp(item.uploaded_at);
        li.appendChild(meta);
        elEvidenceList.appendChild(li);
      });
      elEvidence.hidden = false;
    } else {
      elEvidence.hidden = true;
    }

    if (issue.status === 'RESOLVED') {
      elReopenSection.hidden = false;
      elReopenFeedback.textContent = '';
      elReopenFeedback.className = 'action-feedback';
      if (issue.active_reopen_request) {
        elReopenFormWrapper.hidden = true;
        elReopenStatus.hidden = false;
        elReopenStatusText.textContent = window.CivicSyncI18n.t('reopen_pending_message');
      } else {
        elReopenFormWrapper.hidden = false;
        elReopenStatus.hidden = true;
        elReopenReason.value = '';
      }
      elReopenSubmitBtn.onclick = () => handleReopenSubmit(issue.public_id);
    } else {
      elReopenSection.hidden = true;
    }

    showState(resultState);
  }

  async function handleReopenSubmit(publicId) {
    const reason = elReopenReason.value.trim();
    if (reason.length < 3) {
      elReopenFeedback.className = 'action-feedback is-error';
      elReopenFeedback.textContent = window.CivicSyncI18n.t('reopen_error_too_short');
      return;
    }
    elReopenSubmitBtn.disabled = true;
    elReopenFeedback.className = 'action-feedback';
    elReopenFeedback.textContent = window.CivicSyncI18n.t('report_submitting_button');
    try {
      await CivicSyncApi.requestReopen(publicId, reason);
      await loadIssue(publicId); // refresh with the real, post-submission pending state
    } catch (err) {
      elReopenFeedback.className = 'action-feedback is-error';
      elReopenFeedback.textContent = err.message;
    } finally {
      elReopenSubmitBtn.disabled = false;
    }
  }

  async function loadIssue(publicId) {
    showState(loadingState);
    try {
      const issue = await CivicSyncApi.getTracking(publicId);
      renderIssue(issue);
    } catch (err) {
      if (err.type === 'not_found') {
        notFoundIdEl.textContent = publicId;
        showState(notFoundState);
      } else if (err.type === 'validation') {
        showState(invalidState);
      } else {
        errorMessageEl.textContent = err.message;
        showState(errorState);
      }
    }
  }

  searchForm.addEventListener('submit', (event) => {
    event.preventDefault();
    const value = searchInput.value.trim().toUpperCase();
    if (!value) return;
    window.location.href = `/track/${encodeURIComponent(value)}`;
  });

  const idFromPath = extractPublicIdFromPath();
  if (idFromPath) {
    searchInput.value = idFromPath;
    loadIssue(idFromPath);
  } else {
    showState(emptyState);
  }
})();
