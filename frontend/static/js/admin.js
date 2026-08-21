/**
 * Authority Command Center placeholder (admin.html). Milestone 11 scope
 * is the citizen flow; this page is only the structural foundation for
 * later milestones. The few numbers it does show are real, pulled live
 * from GET /api/admin/dashboard/summary -- nothing here is fabricated.
 */
(function () {
  const { CivicSyncApi } = window;

  const kpiTotal = document.getElementById('kpi-total');
  const kpiSubmitted = document.getElementById('kpi-submitted');
  const kpiInProgress = document.getElementById('kpi-in-progress');
  const kpiResolved = document.getElementById('kpi-resolved');
  const errorNotice = document.getElementById('admin-error');

  async function loadSummary() {
    try {
      const summary = await CivicSyncApi.getDashboardSummary();
      kpiTotal.textContent = summary.total_issues;
      kpiSubmitted.textContent = summary.by_status.SUBMITTED ?? 0;
      kpiInProgress.textContent = summary.by_status.IN_PROGRESS ?? 0;
      kpiResolved.textContent = summary.by_status.RESOLVED ?? 0;
    } catch (err) {
      errorNotice.hidden = false;
      errorNotice.textContent = err.message;
    }
  }

  loadSummary();
})();
