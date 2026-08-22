/**
 * Authority Command Center (admin.html), Milestone 12.
 *
 * Single-page, hash-routed views (Dashboard / Queue / All Issues / Stale /
 * Departments / Civic Intelligence) plus an issue-detail modal reachable
 * from any list. Every network call goes through CivicSyncApi -- nothing
 * here calls fetch() directly. No client-side data invention: every
 * number, chart, and table row comes straight from an existing backend
 * endpoint's response.
 *
 * AI/authority boundary: this file never lets Gemini's advisory output
 * (POST /api/admin/insights/prioritize) drive an assignment or lifecycle
 * action -- those only ever happen through explicit buttons that call the
 * real assignment/status endpoints, with the AI panel visually and
 * textually marked "AI Advisory -- Not an Official Decision".
 */
(function () {
  const { CivicSyncApi, CivicSyncUtils } = window;

  // --- Known vocabularies not exposed by any GET endpoint ----------------------
  // IssueStatus and IssueCategory are fixed backend enums (backend/models.py,
  // ai/schemas.py) with no "list values" API. These are copied verbatim from
  // those enums for filter dropdowns only -- never used to fabricate data,
  // only to let an authority pick a real, valid filter value.
  const ISSUE_STATUSES = [
    'SUBMITTED', 'CLASSIFIED', 'ROUTED', 'ACKNOWLEDGED', 'IN_PROGRESS',
    'RESOLVED', 'CLOSED', 'REOPENED', 'REJECTED',
  ];
  const ISSUE_CATEGORIES = [
    'Street Lighting', 'Roads and Potholes', 'Water Supply', 'Sewage and Drainage',
    'Sanitation and Waste', 'Electricity', 'Public Safety', 'Public Transport',
    'Parks and Public Spaces', 'Noise Pollution', 'Illegal Construction',
    'Stray Animals', 'Other',
  ];
  const TERMINAL_STATUSES = new Set(['CLOSED', 'REJECTED']);

  // UI-ONLY convenience copy of backend/transitions.py's VALID_TRANSITIONS,
  // used only to decide which lifecycle buttons to show. The backend
  // (backend/transitions.py) remains the sole authority -- every transition
  // is still validated server-side on submit, and a button appearing here
  // is not a guarantee the API will accept it.
  const VALID_TRANSITIONS = {
    SUBMITTED: ['CLASSIFIED', 'REJECTED'],
    CLASSIFIED: ['ROUTED', 'REJECTED'],
    ROUTED: ['ACKNOWLEDGED'],
    ACKNOWLEDGED: ['IN_PROGRESS'],
    IN_PROGRESS: ['RESOLVED'],
    RESOLVED: ['CLOSED', 'REOPENED'],
    REOPENED: ['IN_PROGRESS'],
    CLOSED: [],
    REJECTED: [],
  };

  // --- Shared state --------------------------------------------------------------
  const state = {
    view: 'dashboard',
    departments: [], // cached GET /api/departments result
    // Set once at boot by loadJurisdictionContext() to the real, current
    // LOCAL_BODY jurisdiction's code (e.g. "IN-WB-NADIA-KRISHNANAGAR"), or
    // null if none is configured. Every view below passes this through
    // to its API calls so the whole Command Center operates within that
    // jurisdiction's real scope -- not merely displaying it (Milestone 14).
    currentJurisdictionCode: null,
    queue: { department_code: '', severity: '', offset: 0, limit: 20, total: 0 },
    issues: {
      status: '', department_code: '', severity: '', category: '',
      sort_by: 'updated_at', sort_order: 'desc', offset: 0, limit: 20, total: 0,
    },
    stale: { older_than_hours: 48, department_code: '', status: '', offset: 0, limit: 20, total: 0 },
    currentModalIssueId: null,
  };

  // --- Element refs ----------------------------------------------------------------
  const globalError = document.getElementById('admin-global-error');
  const navLinks = Array.from(document.querySelectorAll('#authority-nav a[data-view]'));
  const views = {
    dashboard: document.getElementById('view-dashboard'),
    queue: document.getElementById('view-queue'),
    issues: document.getElementById('view-issues'),
    stale: document.getElementById('view-stale'),
    departments: document.getElementById('view-departments'),
    intelligence: document.getElementById('view-intelligence'),
  };

  function showGlobalError(message) {
    if (!message) {
      globalError.hidden = true;
      globalError.textContent = '';
      return;
    }
    globalError.hidden = false;
    globalError.textContent = message;
  }

  // --- View routing ------------------------------------------------------------------

  function setActiveView(viewName) {
    if (!views[viewName]) viewName = 'dashboard';
    state.view = viewName;
    Object.entries(views).forEach(([name, el]) => el.classList.toggle('is-active', name === viewName));
    navLinks.forEach((a) => a.classList.toggle('active', a.dataset.view === viewName));
    loadView(viewName);
  }

  function loadView(viewName) {
    if (viewName === 'dashboard') loadDashboard();
    else if (viewName === 'queue') loadQueue();
    else if (viewName === 'issues') loadIssues();
    else if (viewName === 'stale') loadStale();
    else if (viewName === 'departments') loadDepartments();
    else if (viewName === 'intelligence') loadIntelligence();
  }

  navLinks.forEach((a) => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      window.location.hash = a.dataset.view;
    });
  });
  window.addEventListener('hashchange', () => {
    const view = (window.location.hash || '#dashboard').slice(1);
    setActiveView(view);
  });

  // --- Generic render helpers -------------------------------------------------------

  function renderBarViz(container, rows, options) {
    options = options || {};
    if (!rows.length) {
      container.innerHTML = '<p class="text-muted type-body-sm">No data yet.</p>';
      return;
    }
    const max = Math.max(1, ...rows.map((r) => r.count));
    container.innerHTML = rows
      .map((r) => {
        const pct = Math.round((r.count / max) * 100);
        const fillClass = options.severityColors ? `sev-${(r.key || r.label).toLowerCase()}` : '';
        return `
          <div class="bar-viz__row">
            <span class="bar-viz__label" title="${CivicSyncUtils.escapeHtml(r.label)}">${CivicSyncUtils.escapeHtml(r.label)}</span>
            <span class="bar-viz__track"><span class="bar-viz__fill ${fillClass}" style="width:${pct}%"></span></span>
            <span class="bar-viz__count">${r.count}</span>
          </div>`;
      })
      .join('');
  }

  function issueRowHtml(item) {
    const deptName = item.assigned_department ? item.assigned_department.name : '\u2014 Unassigned';
    return `
      <tr class="is-clickable" data-public-id="${CivicSyncUtils.escapeHtml(item.public_id)}">
        <td class="cell-id">${CivicSyncUtils.escapeHtml(item.public_id)}</td>
        <td>${CivicSyncUtils.escapeHtml(item.category)}</td>
        <td><span class="chip ${CivicSyncUtils.severityChipClass(item.severity)}">${CivicSyncUtils.escapeHtml(item.severity)}</span></td>
        <td><span class="chip ${CivicSyncUtils.statusChipClass(item.status)}">${CivicSyncUtils.escapeHtml(item.status_label)}</span></td>
        <td class="cell-muted">${CivicSyncUtils.escapeHtml(deptName)}</td>
        <td class="cell-muted">${CivicSyncUtils.formatTimestamp(item.updated_at)}</td>
      </tr>`;
  }

  function renderIssueTable(tbody, items) {
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="table-empty-state">No issues match these filters.</td></tr>';
      return;
    }
    tbody.innerHTML = items.map(issueRowHtml).join('');
    tbody.querySelectorAll('tr[data-public-id]').forEach((row) => {
      row.addEventListener('click', () => openIssueDetail(row.dataset.publicId));
    });
  }

  function renderPagination(summaryEl, prevBtn, nextBtn, filters, onChange) {
    const shown = filters.total === 0 ? 0 : Math.min(filters.limit, filters.total - filters.offset);
    const from = filters.total === 0 ? 0 : filters.offset + 1;
    const to = filters.offset + shown;
    summaryEl.textContent = `Showing ${from}\u2013${to} of ${filters.total}`;
    prevBtn.disabled = filters.offset <= 0;
    nextBtn.disabled = filters.offset + filters.limit >= filters.total;
    prevBtn.onclick = () => {
      filters.offset = Math.max(0, filters.offset - filters.limit);
      onChange();
    };
    nextBtn.onclick = () => {
      filters.offset = filters.offset + filters.limit;
      onChange();
    };
  }

  function populateSelect(selectEl, values, currentValue) {
    const placeholder = selectEl.options[0]; // keep the "All" option
    selectEl.innerHTML = '';
    selectEl.appendChild(placeholder);
    values.forEach((v) => {
      const opt = document.createElement('option');
      opt.value = typeof v === 'string' ? v : v.value;
      opt.textContent = typeof v === 'string' ? v : v.label;
      selectEl.appendChild(opt);
    });
    if (currentValue) selectEl.value = currentValue;
  }

  async function ensureDepartmentsLoaded() {
    if (state.departments.length) return state.departments;
    try {
      state.departments = await CivicSyncApi.getDepartments();
    } catch (err) {
      state.departments = [];
    }
    return state.departments;
  }

  // ================================================================================
  // DASHBOARD
  // ================================================================================

  async function loadDashboard() {
    showGlobalError(null);
    const [summaryR, funnelR, severityR, workloadR, agingR, activityR] = await Promise.allSettled([
      CivicSyncApi.getDashboardSummary({ jurisdiction_code: state.currentJurisdictionCode || null }),
      CivicSyncApi.getFunnel(state.currentJurisdictionCode),
      CivicSyncApi.getSeverityDistribution(state.currentJurisdictionCode),
      CivicSyncApi.getDepartmentWorkload(state.currentJurisdictionCode),
      CivicSyncApi.getAgingBuckets(state.currentJurisdictionCode),
      CivicSyncApi.getRecentActivity(10, state.currentJurisdictionCode),
    ]);

    if (summaryR.status === 'fulfilled') {
      const s = summaryR.value;
      const active = Object.entries(s.by_status)
        .filter(([status]) => !TERMINAL_STATUSES.has(status))
        .reduce((sum, [, count]) => sum + count, 0);
      const resolvedClosed = (s.by_status.RESOLVED || 0) + (s.by_status.CLOSED || 0);
      const highCritical =
        (s.by_severity.High || 0) + (s.by_severity.Critical || 0);
      document.getElementById('kpi-total').textContent = s.total_issues;
      document.getElementById('kpi-active').textContent = active;
      document.getElementById('kpi-resolved-closed').textContent = resolvedClosed;
      document.getElementById('kpi-high-critical').textContent = highCritical;
    } else {
      showGlobalError('Could not load dashboard summary: ' + summaryR.reason.message);
    }

    if (funnelR.status === 'fulfilled') {
      renderBarViz(
        document.getElementById('funnel-viz'),
        funnelR.value.stages.map((s) => ({ label: s.status_label, count: s.count }))
      );
    }

    if (severityR.status === 'fulfilled') {
      renderBarViz(
        document.getElementById('severity-viz'),
        severityR.value.distribution.map((s) => ({ label: `${s.severity} (${s.percentage}%)`, count: s.count, key: s.severity })),
        { severityColors: true }
      );
    }

    if (workloadR.status === 'fulfilled') {
      renderBarViz(
        document.getElementById('dept-workload-viz'),
        workloadR.value.departments.map((d) => ({ label: d.name, count: d.total_assigned }))
      );
    }

    if (agingR.status === 'fulfilled') {
      renderBarViz(
        document.getElementById('aging-viz'),
        agingR.value.buckets.map((b) => ({ label: b.bucket, count: b.count }))
      );
    }

    if (activityR.status === 'fulfilled') {
      renderRecentActivity(activityR.value.activities);
    }
  }

  function renderRecentActivity(activities) {
    const el = document.getElementById('recent-activity-list');
    if (!activities.length) {
      el.innerHTML = '<p class="text-muted type-body-sm">No activity yet.</p>';
      return;
    }
    el.innerHTML = activities
      .map((a) => {
        const from = a.from_status ? CivicSyncUtils.statusLabel(a.from_status) : 'New';
        return `
          <div class="history-row">
            <span>
              <strong class="cell-id">${CivicSyncUtils.escapeHtml(a.public_id)}</strong>
              — ${CivicSyncUtils.escapeHtml(a.category)}: ${from} \u2192 ${CivicSyncUtils.escapeHtml(CivicSyncUtils.statusLabel(a.to_status))}
            </span>
            <span class="history-row__meta">${CivicSyncUtils.formatTimestamp(a.changed_at)}</span>
          </div>`;
      })
      .join('');
  }

  document.getElementById('dashboard-refresh').addEventListener('click', loadDashboard);

  // ================================================================================
  // ISSUE QUEUE
  // ================================================================================

  async function loadQueue() {
    await ensureDepartmentsLoaded();
    populateSelect(
      document.getElementById('queue-filter-department'),
      state.departments.map((d) => ({ value: d.code, label: d.name })),
      state.queue.department_code
    );
    try {
      const response = await CivicSyncApi.getOperationalQueue({
        department_code: state.queue.department_code || null,
        severity: state.queue.severity || null,
        jurisdiction_code: state.currentJurisdictionCode || null,
        limit: state.queue.limit,
        offset: state.queue.offset,
      });
      state.queue.total = response.total;
      renderIssueTable(document.getElementById('queue-table-body'), response.items);
      renderPagination(
        document.getElementById('queue-pagination-summary'),
        document.getElementById('queue-prev'),
        document.getElementById('queue-next'),
        state.queue,
        loadQueue
      );
    } catch (err) {
      showGlobalError('Could not load the issue queue: ' + err.message);
    }
  }

  document.getElementById('queue-apply-filters').addEventListener('click', () => {
    state.queue.department_code = document.getElementById('queue-filter-department').value;
    state.queue.severity = document.getElementById('queue-filter-severity').value;
    state.queue.offset = 0;
    loadQueue();
  });

  // ================================================================================
  // ALL ISSUES
  // ================================================================================

  async function loadIssues() {
    await ensureDepartmentsLoaded();
    populateSelect(document.getElementById('issues-filter-status'), ISSUE_STATUSES, state.issues.status);
    populateSelect(
      document.getElementById('issues-filter-department'),
      state.departments.map((d) => ({ value: d.code, label: d.name })),
      state.issues.department_code
    );
    populateSelect(document.getElementById('issues-filter-category'), ISSUE_CATEGORIES, state.issues.category);
    try {
      const response = await CivicSyncApi.listAdminIssues({
        status: state.issues.status || null,
        department_code: state.issues.department_code || null,
        severity: state.issues.severity || null,
        category: state.issues.category || null,
        jurisdiction_code: state.currentJurisdictionCode || null,
        sort_by: state.issues.sort_by,
        sort_order: state.issues.sort_order,
        limit: state.issues.limit,
        offset: state.issues.offset,
      });
      state.issues.total = response.total;
      renderIssueTable(document.getElementById('issues-table-body'), response.items);
      renderPagination(
        document.getElementById('issues-pagination-summary'),
        document.getElementById('issues-prev'),
        document.getElementById('issues-next'),
        state.issues,
        loadIssues
      );
    } catch (err) {
      showGlobalError('Could not load issues: ' + err.message);
    }
  }

  document.getElementById('issues-apply-filters').addEventListener('click', () => {
    state.issues.status = document.getElementById('issues-filter-status').value;
    state.issues.department_code = document.getElementById('issues-filter-department').value;
    state.issues.severity = document.getElementById('issues-filter-severity').value;
    state.issues.category = document.getElementById('issues-filter-category').value;
    state.issues.sort_by = document.getElementById('issues-sort-by').value;
    state.issues.sort_order = document.getElementById('issues-sort-order').value;
    state.issues.offset = 0;
    loadIssues();
  });

  // ================================================================================
  // STALE ISSUES
  // ================================================================================

  async function loadStale() {
    await ensureDepartmentsLoaded();
    populateSelect(
      document.getElementById('stale-filter-department'),
      state.departments.map((d) => ({ value: d.code, label: d.name })),
      state.stale.department_code
    );
    populateSelect(document.getElementById('stale-filter-status'), ISSUE_STATUSES, state.stale.status);
    try {
      const response = await CivicSyncApi.getStaleIssues({
        older_than_hours: state.stale.older_than_hours,
        department_code: state.stale.department_code || null,
        status: state.stale.status || null,
        jurisdiction_code: state.currentJurisdictionCode || null,
        limit: state.stale.limit,
        offset: state.stale.offset,
      });
      state.stale.total = response.total;
      renderIssueTable(document.getElementById('stale-table-body'), response.items);
      renderPagination(
        document.getElementById('stale-pagination-summary'),
        document.getElementById('stale-prev'),
        document.getElementById('stale-next'),
        state.stale,
        loadStale
      );
    } catch (err) {
      showGlobalError('Could not load stale issues: ' + err.message);
    }
  }

  document.getElementById('stale-apply-filters').addEventListener('click', () => {
    state.stale.older_than_hours = parseInt(document.getElementById('stale-filter-hours').value, 10) || 48;
    state.stale.department_code = document.getElementById('stale-filter-department').value;
    state.stale.status = document.getElementById('stale-filter-status').value;
    state.stale.offset = 0;
    loadStale();
  });

  // ================================================================================
  // DEPARTMENTS
  // ================================================================================

  async function loadDepartments() {
    const grid = document.getElementById('department-grid');
    document.getElementById('department-detail-card').hidden = true;
    try {
      const workload = await CivicSyncApi.getDepartmentWorkload(state.currentJurisdictionCode);
      if (!workload.departments.length) {
        grid.innerHTML = '<p class="text-muted">No active departments configured.</p>';
        return;
      }
      grid.innerHTML = workload.departments
        .map(
          (d) => `
        <div class="department-card" data-code="${CivicSyncUtils.escapeHtml(d.code)}">
          <div class="department-card__name">${CivicSyncUtils.escapeHtml(d.name)}</div>
          <div class="department-card__stats">
            <div><strong>${d.active_issues}</strong>Active</div>
            <div><strong>${d.total_assigned}</strong>Total</div>
          </div>
        </div>`
        )
        .join('');
      grid.querySelectorAll('.department-card').forEach((card) => {
        card.addEventListener('click', () => openDepartmentDetail(card.dataset.code));
      });
    } catch (err) {
      showGlobalError('Could not load department workload: ' + err.message);
    }
  }

  async function openDepartmentDetail(code) {
    try {
      const summary = await CivicSyncApi.getDepartmentSummary(code);
      document.getElementById('department-detail-name').textContent = summary.name;
      document.getElementById('dept-detail-total').textContent = summary.total_assigned_issues;
      document.getElementById('dept-detail-active').textContent = summary.active_issues;
      document.getElementById('dept-detail-resolved').textContent = summary.resolved_issues;
      document.getElementById('dept-detail-closed').textContent = summary.closed_issues;
      renderBarViz(
        document.getElementById('dept-detail-status-viz'),
        Object.entries(summary.by_status).map(([status, count]) => ({
          label: CivicSyncUtils.statusLabel(status),
          count,
        }))
      );
      document.getElementById('department-detail-card').hidden = false;
    } catch (err) {
      showGlobalError('Could not load department detail: ' + err.message);
    }
  }

  // ================================================================================
  // CIVIC INTELLIGENCE
  // ================================================================================

  function renderInsights(insights) {
    const el = document.getElementById('insight-list');
    if (!insights.length) {
      el.innerHTML = '<p class="text-muted">No insights right now \u2014 nothing in the current data meets the threshold for a grounded pattern.</p>';
      return;
    }
    el.innerHTML = insights
      .map((i) => {
        const evidence = Object.entries(i.evidence)
          .map(([k, v]) => `<span class="insight-card__evidence-item">${CivicSyncUtils.escapeHtml(k)}: ${CivicSyncUtils.escapeHtml(v)}</span>`)
          .join('');
        const dept = i.affected_department ? ` \u00b7 ${CivicSyncUtils.escapeHtml(i.affected_department.name)}` : '';
        return `
          <div class="insight-card priority-${CivicSyncUtils.escapeHtml(i.priority)}">
            <div class="insight-card__header">
              <span class="insight-card__title">${CivicSyncUtils.escapeHtml(i.title)}</span>
              <span class="chip ${CivicSyncUtils.insightPriorityChipClass(i.priority)}">${CivicSyncUtils.escapeHtml(i.priority)}</span>
            </div>
            <p class="insight-card__summary">${CivicSyncUtils.escapeHtml(i.summary)}${dept}</p>
            <div class="insight-card__evidence">${evidence}</div>
            <p class="insight-card__action">Suggested: ${CivicSyncUtils.escapeHtml(i.recommended_action)}</p>
          </div>`;
      })
      .join('');
  }

  async function loadIntelligence() {
    document.getElementById('ai-advisory-panel').hidden = true;
    document.getElementById('ai-advisory-error').hidden = true;
    document.getElementById('briefing-result').hidden = true;
    document.getElementById('briefing-error').hidden = true;
    await ensureDepartmentsLoaded();
    populateSelect(
      document.getElementById('briefing-department-select'),
      state.departments.map((d) => ({ value: d.code, label: d.name })),
      ''
    );
    try {
      const response = await CivicSyncApi.getInsights(state.currentJurisdictionCode);
      renderInsights(response.insights);
    } catch (err) {
      showGlobalError('Could not load civic insights: ' + err.message);
    }
  }

  document.getElementById('get-ai-advisory').addEventListener('click', async () => {
    const btn = document.getElementById('get-ai-advisory');
    btn.disabled = true;
    btn.textContent = 'Asking Gemini\u2026';
    try {
      const response = await CivicSyncApi.prioritizeInsights(state.currentJurisdictionCode);
      renderInsights(response.insights); // grounded insights, unchanged by the AI step
      const panel = document.getElementById('ai-advisory-panel');
      const errorEl = document.getElementById('ai-advisory-error');
      if (response.ai_recommendation) {
        document.getElementById('ai-advisory-summary').textContent = response.ai_recommendation.summary;
        document.getElementById('ai-advisory-explanation').textContent = response.ai_recommendation.explanation;
        document.getElementById('ai-advisory-order').innerHTML = response.ai_recommendation.recommended_priority_order
          .map((t) => `<li>${CivicSyncUtils.escapeHtml(t.replace(/_/g, ' '))}</li>`)
          .join('');
        panel.hidden = false;
        errorEl.hidden = true;
      } else {
        panel.hidden = true;
        errorEl.hidden = false;
        errorEl.textContent =
          response.ai_recommendation_error ||
          'AI advisory is unavailable right now. The grounded insights above are still accurate.';
      }
    } catch (err) {
      document.getElementById('ai-advisory-panel').hidden = true;
      const errorEl = document.getElementById('ai-advisory-error');
      errorEl.hidden = false;
      errorEl.textContent = 'AI advisory is unavailable right now. The grounded insights above are still accurate.';
    } finally {
      btn.disabled = false;
      btn.textContent = 'Get AI Advisory';
    }
  });

  document.getElementById('generate-briefing-btn').addEventListener('click', async () => {
    const btn = document.getElementById('generate-briefing-btn');
    const resultPanel = document.getElementById('briefing-result');
    const errorEl = document.getElementById('briefing-error');
    const departmentCode = document.getElementById('briefing-department-select').value || null;

    if (!state.currentJurisdictionCode) {
      errorEl.hidden = false;
      errorEl.textContent = 'No jurisdiction is currently configured to brief on.';
      return;
    }

    btn.disabled = true;
    btn.textContent = 'Asking Gemini\u2026';
    resultPanel.hidden = true;
    errorEl.hidden = true;

    try {
      const response = await CivicSyncApi.generateOperationalBriefing(
        state.currentJurisdictionCode,
        departmentCode
      );
      if (response.briefing) {
        document.getElementById('briefing-text').textContent = response.briefing;
        document.getElementById('briefing-observations').innerHTML = response.key_observations
          .map((o) => `<li>${CivicSyncUtils.escapeHtml(o)}</li>`)
          .join('');
        document.getElementById('briefing-signals').innerHTML = response.priority_signals
          .map((s) => `<li>${CivicSyncUtils.escapeHtml(s)}</li>`)
          .join('');
        document.getElementById('briefing-considerations').innerHTML = response.considerations
          .map((c) => `<li>${CivicSyncUtils.escapeHtml(c)}</li>`)
          .join('');
        resultPanel.hidden = false;
      } else {
        errorEl.hidden = false;
        errorEl.textContent =
          response.error || 'AI operational briefing is unavailable right now.';
      }
    } catch (err) {
      errorEl.hidden = false;
      errorEl.textContent = err.message;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Generate Operational Briefing';
    }
  });

  // ================================================================================
  // ISSUE DETAIL MODAL
  // ================================================================================

  const modalOverlay = document.getElementById('issue-modal-overlay');
  const modalBody = document.getElementById('modal-body');

  function closeModal() {
    modalOverlay.hidden = true;
    state.currentModalIssueId = null;
  }
  document.getElementById('modal-close').addEventListener('click', closeModal);
  modalOverlay.addEventListener('click', (e) => {
    if (e.target === modalOverlay) closeModal();
  });

  async function openIssueDetail(publicId) {
    state.currentModalIssueId = publicId;
    modalOverlay.hidden = false;
    document.getElementById('modal-public-id').textContent = publicId;
    document.getElementById('modal-category').textContent = '';
    modalBody.innerHTML = '<div class="skeleton" style="height: 300px;"></div>';

    try {
      const detail = await CivicSyncApi.getAdminIssueDetail(publicId);
      if (state.currentModalIssueId !== publicId) return; // closed/reopened for a different issue meanwhile
      document.getElementById('modal-category').textContent = detail.category;
      renderIssueDetail(detail);
    } catch (err) {
      modalBody.innerHTML = `<div class="notice notice-error">${CivicSyncUtils.escapeHtml(err.message)}</div>`;
    }
  }

  async function renderIssueDetail(detail) {
    await ensureDepartmentsLoaded();
    const confidencePct = Math.round(detail.confidence * 100);
    const nextStatuses = VALID_TRANSITIONS[detail.status] || [];

    const statusHistoryHtml = detail.status_history
      .map(
        (h) => `
        <div class="history-row">
          <span>${h.from_status ? CivicSyncUtils.statusLabel(h.from_status) + ' \u2192 ' : ''}${CivicSyncUtils.statusLabel(h.to_status)}${h.reason ? ' \u2014 ' + CivicSyncUtils.escapeHtml(h.reason) : ''}</span>
          <span class="history-row__meta">${CivicSyncUtils.formatTimestamp(h.changed_at)}</span>
        </div>`
      )
      .join('') || '<p class="text-muted type-body-sm">No history yet.</p>';

    const assignmentHistoryHtml = detail.assignment_history
      .map(
        (a) => `
        <div class="history-row">
          <span>${CivicSyncUtils.escapeHtml(a.department_name)}${a.unassigned_at ? ' (ended)' : ' (current)'}${a.reason ? ' \u2014 ' + CivicSyncUtils.escapeHtml(a.reason) : ''}</span>
          <span class="history-row__meta">${CivicSyncUtils.formatTimestamp(a.assigned_at)}</span>
        </div>`
      )
      .join('') || '<p class="text-muted type-body-sm">Not yet assigned.</p>';

    const evidenceListHtml = detail.evidence
      .map(
        (e) => `
        <div class="history-row">
          <span><a href="/api/evidence/${encodeURIComponent(e.public_id)}/file" target="_blank" rel="noopener">${CivicSyncUtils.escapeHtml(e.original_filename)}</a> \u2014 ${CivicSyncUtils.escapeHtml(e.uploaded_by)}</span>
          <span class="history-row__meta">${CivicSyncUtils.formatTimestamp(e.uploaded_at)}</span>
        </div>`
      )
      .join('') || '<p class="text-muted type-body-sm">No evidence attached yet.</p>';

    const departmentOptions = state.departments
      .map((d) => `<option value="${CivicSyncUtils.escapeHtml(d.code)}">${CivicSyncUtils.escapeHtml(d.name)}</option>`)
      .join('');

    const transitionButtonsHtml = nextStatuses.length
      ? nextStatuses
          .map(
            (s) =>
              `<button class="btn btn-secondary transition-btn" data-status="${s}">Move to ${CivicSyncUtils.escapeHtml(CivicSyncUtils.statusLabel(s))}</button>`
          )
          .join('')
      : '<p class="text-muted type-body-sm">No further lifecycle transitions available from this status.</p>';

    modalBody.innerHTML = `
      <div class="detail-section">
        <div class="detail-section-title"><h3 class="type-headline-sm" style="font-size:16px;">Citizen Report</h3></div>
        <p class="mb-sm">${CivicSyncUtils.escapeHtml(detail.original_text)}</p>
        <dl class="detail-grid">
          <div><dt>Location</dt><dd>${CivicSyncUtils.escapeHtml(detail.location || 'Not specified')}</dd></div>
          <div><dt>Duration</dt><dd>${CivicSyncUtils.escapeHtml(detail.duration || 'Not specified')}</dd></div>
          <div><dt>Affected Population</dt><dd>${CivicSyncUtils.escapeHtml(detail.affected_population || 'Not specified')}</dd></div>
        </dl>
      </div>

      <div class="detail-section">
        <div class="detail-section-title"><h3 class="type-headline-sm" style="font-size:16px;">AI Analysis</h3></div>
        <div class="ai-panel">
          <span class="ai-panel__label">🤖 AI-Derived \u2014 Not Official</span>
          <dl class="detail-grid">
            <div><dt>Severity</dt><dd><span class="chip ${CivicSyncUtils.severityChipClass(detail.severity)}">${CivicSyncUtils.escapeHtml(detail.severity)}</span></dd></div>
            <div><dt>AI Suggested Department</dt><dd>${CivicSyncUtils.escapeHtml(detail.suggested_department || 'Not determined')}</dd></div>
            <div><dt>AI Confidence</dt><dd>${confidencePct}%</dd></div>
          </dl>

          <button class="btn btn-secondary mt-sm" id="explain-with-ai-btn" type="button">
            Explain with AI
          </button>
          <div id="ai-explanation-result" class="mt-sm" hidden>
            <p class="ai-panel__label" style="margin-bottom: 4px;">🤖 AI Explanation \u2014 Advisory Only</p>
            <p id="ai-explanation-text" class="type-body-sm"></p>
            <ul id="ai-explanation-considerations" class="type-body-sm" style="margin: 6px 0 0 18px; padding: 0;"></ul>
          </div>
          <p id="ai-explanation-error" class="action-feedback is-error" hidden></p>
        </div>
      </div>

      <div class="detail-section">
        <div class="detail-section-title"><h3 class="type-headline-sm" style="font-size:16px;">Official Authority State</h3></div>
        <div class="official-panel">
          <span class="official-panel__label">\u2713 Official</span>
          <dl class="detail-grid">
            <div><dt>Status</dt><dd><span class="chip ${CivicSyncUtils.statusChipClass(detail.status)}">${CivicSyncUtils.escapeHtml(detail.status_label)}</span></dd></div>
            <div><dt>Assigned Department</dt><dd>${detail.assigned_department ? CivicSyncUtils.escapeHtml(detail.assigned_department.name) : 'Not yet assigned'}</dd></div>
            <div><dt>Created</dt><dd>${CivicSyncUtils.formatTimestamp(detail.created_at)}</dd></div>
            <div><dt>Last Updated</dt><dd>${CivicSyncUtils.formatTimestamp(detail.updated_at)}</dd></div>
            <div><dt>Resolved</dt><dd>${detail.resolved_at ? CivicSyncUtils.formatTimestamp(detail.resolved_at) : '\u2014'}</dd></div>
            <div><dt>Closed</dt><dd>${detail.closed_at ? CivicSyncUtils.formatTimestamp(detail.closed_at) : '\u2014'}</dd></div>
          </dl>
        </div>
      </div>

      <div class="detail-section">
        <div class="detail-section-title"><h3 class="type-headline-sm" style="font-size:16px;">Assign / Reassign Department</h3></div>
        <div class="action-row">
          <div class="filter-field">
            <label for="assign-department-select">Department</label>
            <select id="assign-department-select"><option value="">Choose a department\u2026</option>${departmentOptions}</select>
          </div>
          <div class="filter-field">
            <label for="assign-reason-input">Reason (optional)</label>
            <input type="text" id="assign-reason-input" placeholder="e.g. Routed to responsible team" />
          </div>
          <button class="btn btn-primary" id="assign-submit-btn">Assign</button>
        </div>
        <div class="action-feedback" id="assign-feedback"></div>
      </div>

      <div class="detail-section">
        <div class="detail-section-title"><h3 class="type-headline-sm" style="font-size:16px;">Lifecycle Actions</h3></div>
        <div class="transition-buttons">${transitionButtonsHtml}</div>
        <div class="action-feedback" id="transition-feedback"></div>
      </div>

      <div class="detail-section">
        <div class="detail-section-title"><h3 class="type-headline-sm" style="font-size:16px;">Resolution Evidence</h3></div>
        <div class="history-list" id="evidence-list">${evidenceListHtml}</div>
        <div class="action-row mt-sm">
          <input type="file" id="evidence-file-input" accept="image/jpeg,image/png,image/webp" />
          <button class="btn btn-secondary" id="evidence-upload-btn" type="button">Upload Evidence</button>
        </div>
        <div class="action-feedback" id="evidence-feedback"></div>
      </div>

      <div class="detail-section">
        <div class="detail-section-title"><h3 class="type-headline-sm" style="font-size:16px;">Status History</h3></div>
        <div class="history-list">${statusHistoryHtml}</div>
      </div>

      <div class="detail-section">
        <div class="detail-section-title"><h3 class="type-headline-sm" style="font-size:16px;">Assignment History</h3></div>
        <div class="history-list">${assignmentHistoryHtml}</div>
      </div>
    `;

    document.getElementById('assign-submit-btn').addEventListener('click', () => handleAssign(detail.public_id));
    document.getElementById('evidence-upload-btn').addEventListener('click', () => handleEvidenceUpload(detail.public_id));
    modalBody.querySelectorAll('.transition-btn').forEach((btn) => {
      btn.addEventListener('click', () => handleTransition(detail.public_id, btn.dataset.status));
    });
    document.getElementById('explain-with-ai-btn').addEventListener('click', () => handleExplain(detail.public_id));
  }

  async function handleExplain(publicId) {
    const btn = document.getElementById('explain-with-ai-btn');
    const resultEl = document.getElementById('ai-explanation-result');
    const textEl = document.getElementById('ai-explanation-text');
    const considerationsEl = document.getElementById('ai-explanation-considerations');
    const errorEl = document.getElementById('ai-explanation-error');

    btn.disabled = true;
    btn.textContent = 'Asking Gemini\u2026';
    resultEl.hidden = true;
    errorEl.hidden = true;

    try {
      const response = await CivicSyncApi.explainIssue(publicId);
      // This is a read-only, advisory, never-persisted call -- it cannot
      // change official state, so there is nothing to refresh here
      // (unlike handleAssign/handleTransition, which do refresh the
      // modal + active list after a real state change).
      if (response.explanation) {
        textEl.textContent = response.explanation;
        considerationsEl.innerHTML = (response.considerations || [])
          .map((c) => `<li>${CivicSyncUtils.escapeHtml(c)}</li>`)
          .join('');
        resultEl.hidden = false;
      } else {
        errorEl.textContent =
          response.error || 'AI explanation is temporarily unavailable.';
        errorEl.hidden = false;
      }
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.hidden = false;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Explain with AI';
    }
  }

  async function handleAssign(publicId) {
    const select = document.getElementById('assign-department-select');
    const reasonInput = document.getElementById('assign-reason-input');
    const feedback = document.getElementById('assign-feedback');
    const btn = document.getElementById('assign-submit-btn');

    if (!select.value) {
      feedback.className = 'action-feedback is-error';
      feedback.textContent = 'Choose a department first.';
      return;
    }

    btn.disabled = true;
    feedback.className = 'action-feedback';
    feedback.textContent = 'Assigning\u2026';
    try {
      await CivicSyncApi.assignDepartment(publicId, select.value, reasonInput.value.trim());
      feedback.className = 'action-feedback is-success';
      feedback.textContent = 'Assignment updated.';
      await openIssueDetail(publicId); // refresh modal with the real, post-assignment state
      refreshActiveListView();
    } catch (err) {
      feedback.className = 'action-feedback is-error';
      feedback.textContent = err.message;
    } finally {
      btn.disabled = false;
    }
  }

  async function handleEvidenceUpload(publicId) {
    const fileInput = document.getElementById('evidence-file-input');
    const feedback = document.getElementById('evidence-feedback');
    const btn = document.getElementById('evidence-upload-btn');

    const file = fileInput.files[0];
    if (!file) {
      feedback.className = 'action-feedback is-error';
      feedback.textContent = 'Choose an image file first.';
      return;
    }

    btn.disabled = true;
    feedback.className = 'action-feedback';
    feedback.textContent = 'Uploading\u2026';
    try {
      await CivicSyncApi.uploadEvidence(publicId, file);
      feedback.className = 'action-feedback is-success';
      feedback.textContent = 'Evidence uploaded.';
      fileInput.value = '';
      await openIssueDetail(publicId); // refresh modal with the real, post-upload evidence list
    } catch (err) {
      feedback.className = 'action-feedback is-error';
      feedback.textContent = err.message;
    } finally {
      btn.disabled = false;
    }
  }

  async function handleTransition(publicId, newStatus) {
    const label = CivicSyncUtils.statusLabel(newStatus);
    if (!window.confirm(`Move this issue to "${label}"? This calls the real CivicSync lifecycle API.`)) {
      return;
    }
    const feedback = document.getElementById('transition-feedback');
    feedback.className = 'action-feedback';
    feedback.textContent = 'Updating\u2026';
    try {
      await CivicSyncApi.transitionStatus(publicId, newStatus, null);
      feedback.className = 'action-feedback is-success';
      feedback.textContent = `Moved to ${label}.`;
      await openIssueDetail(publicId); // refresh modal with the real, post-transition state
      refreshActiveListView();
    } catch (err) {
      feedback.className = 'action-feedback is-error';
      feedback.textContent = err.message;
    }
  }

  function refreshActiveListView() {
    // Refresh whichever list/dashboard is currently visible so operators
    // see the real, updated state immediately -- never assume success.
    if (state.view === 'queue') loadQueue();
    else if (state.view === 'issues') loadIssues();
    else if (state.view === 'stale') loadStale();
    else if (state.view === 'dashboard') loadDashboard();
  }

  // ================================================================================
  // JURISDICTION CONTEXT (Milestone 13 display, Milestone 14 operational scoping)
  // ================================================================================
  //
  // Real, backend-driven, read-only context -- not a decorative selector.
  // Shows the one real jurisdiction chain currently configured (e.g.
  // "India / West Bengal / Nadia / Krishnanagar Municipality"), sourced
  // entirely from GET /api/jurisdictions. If no LOCAL_BODY jurisdiction
  // is configured yet, the panel stays hidden rather than showing a fake
  // placeholder chain.
  //
  // Milestone 14: state.currentJurisdictionCode (set here) is what makes
  // this operational rather than cosmetic -- every dashboard/queue/
  // issues/stale load below passes it through to the real API, so the
  // whole Command Center genuinely operates within this jurisdiction's
  // scope. Proven by tests/test_jurisdiction_scoping.py, which
  // constructs a second, independent jurisdiction and confirms it's
  // correctly excluded when scoped to the first.

  async function loadJurisdictionContext() {
    const panel = document.getElementById('jurisdiction-context');
    const breadcrumbEl = document.getElementById('jurisdiction-breadcrumb');
    try {
      const response = await CivicSyncApi.getJurisdictions({ level: 'LOCAL_BODY' });
      const localBody = response.jurisdictions[0];
      if (!localBody) {
        state.currentJurisdictionCode = null;
        panel.hidden = true;
        return;
      }
      state.currentJurisdictionCode = localBody.code;
      breadcrumbEl.textContent = localBody.ancestry.map((j) => j.name).join(' / ');
      panel.hidden = false;
    } catch (err) {
      state.currentJurisdictionCode = null;
      panel.hidden = true; // fail quietly -- this is context, not core functionality
    }
  }

  document.getElementById('logout-button').addEventListener('click', async () => {
    try {
      await CivicSyncApi.authorityLogout();
    } catch (err) {
      // Even if the logout call itself fails, still send the user to the
      // login page -- there's nothing useful to do with the error here.
    }
    window.location.href = '/authority/login';
  });

  // --- Boot --------------------------------------------------------------------------
  //
  // Jurisdiction context is awaited BEFORE the first view loads, so even
  // the very first render (e.g. the Dashboard on a fresh page load) is
  // already correctly scoped rather than briefly showing unscoped data.

  (async () => {
    await loadJurisdictionContext();
    const initialView = (window.location.hash || '#dashboard').slice(1);
    setActiveView(initialView);
  })();
})();
