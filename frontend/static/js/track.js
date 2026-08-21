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
    el.textContent = value ? value : 'Not specified';
  }

  function renderIssue(issue) {
    elCategory.textContent = issue.category;
    elStatusChip.textContent = issue.status_label;
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
      : 'Not yet assigned \u2014 pending review';
    elCreatedAt.textContent = CivicSyncUtils.formatTimestamp(issue.created_at);
    elUpdatedAt.textContent = CivicSyncUtils.formatTimestamp(issue.updated_at);

    elTimeline.innerHTML = '';
    const timeline = issue.timeline || [];
    timeline.forEach((entry, index) => {
      const isCurrent = index === timeline.length - 1;
      const li = document.createElement('li');
      li.className = `timeline__item${isCurrent ? ' timeline__item--current' : ''}`;
      li.innerHTML = `
        <div class="timeline__status">${CivicSyncUtils.escapeHtml(entry.status)}</div>
        <div class="timeline__timestamp">${CivicSyncUtils.escapeHtml(CivicSyncUtils.formatTimestamp(entry.timestamp))}</div>
        ${entry.reason ? `<div class="timeline__reason">${CivicSyncUtils.escapeHtml(entry.reason)}</div>` : ''}
      `;
      elTimeline.appendChild(li);
    });

    showState(resultState);
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
