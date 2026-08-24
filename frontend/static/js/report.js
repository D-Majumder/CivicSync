/**
 * Citizen report flow (index.html): Report Issue -> AI Analysis ->
 * Submission Result, as one page with JS-driven view switching rather
 * than three separate page loads.
 *
 * IMPORTANT HONESTY NOTE: POST /api/issues is a SINGLE backend request --
 * Gemini analysis and persistence both happen server-side inside that one
 * call. The "stages" shown during the AI Analysis view are a waiting
 * animation for that one request, not four separate API calls. The
 * animation never claims completion of any stage it hasn't actually
 * reached, and the final "Analysis complete" state only appears once the
 * real response has actually come back.
 */
(function () {
  const { CivicSyncApi, CivicSyncUtils } = window;

  const MIN_COMPLAINT_LENGTH = 10;
  const STAGE_ADVANCE_MS = 900;
  const MIN_ANALYSIS_DISPLAY_MS = 1400;

  function getStages() {
    return [
      window.CivicSyncI18n.t('analysis_stage_analyzing'),
      window.CivicSyncI18n.t('analysis_stage_classifying'),
      window.CivicSyncI18n.t('analysis_stage_severity'),
      window.CivicSyncI18n.t('analysis_stage_department'),
    ];
  }

  // --- Element refs -----------------------------------------------------------
  const formStep = document.getElementById('step-form');
  const analysisStep = document.getElementById('step-analysis');
  const resultStep = document.getElementById('step-result');

  const form = document.getElementById('report-form');
  const complaintField = document.getElementById('complaint-text');
  const complaintFieldWrap = document.getElementById('complaint-field-wrap');
  const complaintError = document.getElementById('complaint-error');
  const submitButton = document.getElementById('submit-button');
  const submitButtonLabel = document.getElementById('submit-button-label');
  const formNotice = document.getElementById('form-notice');

  const stageListEl = document.getElementById('analysis-stage-list');
  const analysisHeading = document.getElementById('analysis-heading');
  const analysisCaption = document.getElementById('analysis-caption');
  const analysisIcon = document.getElementById('analysis-icon');
  const analysisOutcome = document.getElementById('analysis-outcome');
  const analysisCategory = document.getElementById('analysis-category');
  const analysisSeverity = document.getElementById('analysis-severity');
  const analysisDepartment = document.getElementById('analysis-department');
  const viewResultButton = document.getElementById('view-result-button');

  const resultPublicId = document.getElementById('result-public-id');
  const resultDepartment = document.getElementById('result-department');
  const resultStatus = document.getElementById('result-status');
  const trackButton = document.getElementById('track-button');
  const reportAnotherButton = document.getElementById('report-another-button');

  const departmentListEl = document.getElementById('department-list');

  const useLocationButton = document.getElementById('use-location-button');
  const useLocationButtonLabel = document.getElementById('use-location-button-label');
  const locationStatus = document.getElementById('location-status');
  const locationAdjustWrap = document.getElementById('location-adjust-wrap');
  const locationLatInput = document.getElementById('location-lat-input');
  const locationLngInput = document.getElementById('location-lng-input');
  const clearLocationButton = document.getElementById('clear-location-button');

  let lastCreatedIssue = null;
  // The captured/adjusted location, or null if none. Only ever set by an
  // explicit citizen action (clicking "Use my current location" and/or
  // editing the resulting fields) -- never fabricated, never defaulted.
  let capturedLocation = null;

  // --- View switching -----------------------------------------------------------

  function showStep(step) {
    [formStep, analysisStep, resultStep].forEach((el) => el.classList.remove('is-active'));
    step.classList.add('is-active');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  // --- Form validation ------------------------------------------------------------

  function setFieldError(message) {
    if (message) {
      complaintFieldWrap.classList.add('has-error');
      complaintError.textContent = message;
    } else {
      complaintFieldWrap.classList.remove('has-error');
      complaintError.textContent = '';
    }
  }

  function validateComplaint() {
    const value = complaintField.value.trim();
    if (value.length === 0) {
      setFieldError(window.CivicSyncI18n.t('report_error_required'));
      return null;
    }
    if (value.length < MIN_COMPLAINT_LENGTH) {
      setFieldError(window.CivicSyncI18n.t('report_error_too_short'));
      return null;
    }
    setFieldError(null);
    return value;
  }

  complaintField.addEventListener('input', () => {
    if (complaintFieldWrap.classList.contains('has-error')) {
      validateComplaint();
    }
  });

  function setFormNotice(message, tone) {
    if (!message) {
      formNotice.hidden = true;
      formNotice.textContent = '';
      formNotice.className = 'notice';
      return;
    }
    formNotice.hidden = false;
    formNotice.className = `notice notice-${tone}`;
    formNotice.textContent = message;
  }

  function setSubmitting(isSubmitting) {
    submitButton.disabled = isSubmitting;
    submitButtonLabel.textContent = isSubmitting
      ? window.CivicSyncI18n.t('report_submitting_button')
      : window.CivicSyncI18n.t('report_submit_button');
    submitButton.querySelector('.spinner')?.remove();
    if (isSubmitting) {
      const spinner = document.createElement('span');
      spinner.className = 'spinner';
      submitButton.prepend(spinner);
    }
  }

  // --- AI Analysis stage animation -------------------------------------------------

  function renderStageList() {
    stageListEl.innerHTML = '';
    getStages().forEach((label) => {
      const item = document.createElement('div');
      item.className = 'analysis-stage';
      item.innerHTML = `<span class="analysis-stage__marker">✓</span><span>${CivicSyncUtils.escapeHtml(label)}</span>`;
      stageListEl.appendChild(item);
    });
  }

  function runStageAnimation() {
    renderStageList();
    const stageEls = Array.from(stageListEl.children);
    let index = 0;
    stageEls[0].classList.add('is-active');

    const intervalId = setInterval(() => {
      // Stop advancing once we reach the last stage -- we never claim
      // "Identifying department" is done until the real response arrives.
      if (index >= stageEls.length - 1) return;
      stageEls[index].classList.remove('is-active');
      stageEls[index].classList.add('is-done');
      index += 1;
      stageEls[index].classList.add('is-active');
    }, STAGE_ADVANCE_MS);

    return {
      finish() {
        clearInterval(intervalId);
        stageEls.forEach((el) => {
          el.classList.remove('is-active');
          el.classList.add('is-done');
        });
      },
    };
  }

  function wait(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // --- Rendering real results -----------------------------------------------------

  function resetAnalysisIcon() {
    analysisIcon.innerHTML = '<span class="spinner spinner-dark"></span>';
    analysisIcon.style.borderColor = '';
    analysisIcon.style.color = '';
  }

  function markAnalysisIconComplete() {
    analysisIcon.textContent = '\u2713';
    analysisIcon.style.borderColor = 'var(--color-success)';
    analysisIcon.style.color = 'var(--color-success)';
  }

  function renderAnalysisOutcome(issue) {
    analysisHeading.textContent = 'Analysis Complete';
    markAnalysisIconComplete();
    analysisCaption.textContent = 'CivicSync\u2019s AI has classified your report.';

    analysisCategory.textContent = issue.category;
    const severityChip = analysisSeverity.querySelector('.chip');
    severityChip.textContent = issue.severity;
    severityChip.className = `chip ${CivicSyncUtils.severityChipClass(issue.severity)}`;
    analysisDepartment.textContent = issue.suggested_department || 'Not determined from the report text';

    analysisOutcome.hidden = false;
  }

  function renderResult(issue) {
    resultPublicId.textContent = issue.public_id;
    resultDepartment.textContent = issue.assigned_department
      ? issue.assigned_department.name
      : window.CivicSyncI18n.t('result_department_pending');
    resultStatus.textContent = window.CivicSyncI18n.statusLabel(issue.status);
    trackButton.setAttribute('href', `/track/${encodeURIComponent(issue.public_id)}`);
  }

  // --- Location capture (Milestone 21) -----------------------------------------
  //
  // Uses the browser's native Geolocation API only -- no third-party
  // location service, no map SDK. Entirely optional: permission denial,
  // an unavailable position, or simply never clicking the button all
  // leave the complaint fully submit-able, exactly like before this
  // milestone. The captured location represents the LOCATION OF THE
  // REPORTED PROBLEM, not necessarily the citizen's own location -- nothing
  // here implies otherwise to the citizen.

  function setLocationStatus(text, isError) {
    locationStatus.textContent = text;
    locationStatus.style.color = isError ? 'var(--color-error)' : '';
  }

  function showCapturedLocation(lat, lng, accuracyMeters) {
    capturedLocation = { latitude: lat, longitude: lng, accuracy: accuracyMeters ?? null };
    locationLatInput.value = lat.toFixed(6);
    locationLngInput.value = lng.toFixed(6);
    locationAdjustWrap.style.display = 'flex';
    locationAdjustWrap.hidden = false;
    const accuracyText =
      accuracyMeters != null ? ` (accuracy \u00b1${Math.round(accuracyMeters)}m)` : '';
    setLocationStatus(window.CivicSyncI18n.t('location_captured', { accuracy: accuracyText }), false);
  }

  useLocationButton.addEventListener('click', () => {
    if (!('geolocation' in navigator)) {
      setLocationStatus(
        window.CivicSyncI18n.t('location_unsupported'),
        true
      );
      return;
    }

    // A non-secure origin (plain HTTP, not localhost) is a very common,
    // easy-to-miss cause of an unexplained "permission denied" with no
    // visible browser prompt at all -- modern browsers refuse
    // geolocation entirely on insecure origins and report it through
    // the exact same PERMISSION_DENIED error code as an actual user
    // denial, with no prompt ever shown. Checking this explicitly lets
    // us tell the citizen the real reason instead of a generic denial
    // message that retrying can never fix.
    if (window.isSecureContext === false) {
      setLocationStatus(window.CivicSyncI18n.t('location_insecure_context'), true);
      return;
    }

    useLocationButton.disabled = true;
    useLocationButtonLabel.textContent = window.CivicSyncI18n.t('location_locating_button');
    setLocationStatus(window.CivicSyncI18n.t('location_requesting'), false);

    navigator.geolocation.getCurrentPosition(
      (position) => {
        useLocationButton.disabled = false;
        useLocationButtonLabel.textContent = window.CivicSyncI18n.t('location_use_button');
        showCapturedLocation(
          position.coords.latitude,
          position.coords.longitude,
          position.coords.accuracy
        );
      },
      (error) => {
        useLocationButton.disabled = false;
        // Re-labeled to make the retry path explicit and discoverable --
        // the button still does exactly the same thing (calls
        // getCurrentPosition again), which succeeds immediately once
        // the citizen has actually changed their browser's site
        // permission, with no page reload needed.
        useLocationButtonLabel.textContent = window.CivicSyncI18n.t('location_try_again_button');
        capturedLocation = null;
        // PERMISSION_DENIED = 1, POSITION_UNAVAILABLE = 2, TIMEOUT = 3 --
        // every case still leaves the complaint fully submit-able. A
        // denial with NO visible prompt (the reported symptom) is
        // standard browser behavior once a site's geolocation
        // permission is already blocked -- the browser does not show a
        // fresh prompt on every subsequent request, it just replays the
        // stored decision. The message below explains this rather than
        // implying a prompt was missed or something is broken.
        if (error.code === error.PERMISSION_DENIED) {
          setLocationStatus(
            window.CivicSyncI18n.t('location_permission_denied'),
            true
          );
        } else {
          setLocationStatus(
            window.CivicSyncI18n.t('location_unavailable'),
            true
          );
        }
      },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 }
    );
  });

  // Manual adjustment: editing the fields directly updates the captured
  // location that will be submitted (still entirely optional, and only
  // ever set from what the citizen has actually entered).
  function handleManualLocationEdit() {
    const lat = parseFloat(locationLatInput.value);
    const lng = parseFloat(locationLngInput.value);
    if (Number.isFinite(lat) && Number.isFinite(lng) && lat >= -90 && lat <= 90 && lng >= -180 && lng <= 180) {
      capturedLocation = { latitude: lat, longitude: lng, accuracy: capturedLocation ? capturedLocation.accuracy : null };
    } else {
      capturedLocation = null;
    }
  }
  locationLatInput.addEventListener('input', handleManualLocationEdit);
  locationLngInput.addEventListener('input', handleManualLocationEdit);

  clearLocationButton.addEventListener('click', () => {
    capturedLocation = null;
    locationLatInput.value = '';
    locationLngInput.value = '';
    locationAdjustWrap.hidden = true;
    setLocationStatus(window.CivicSyncI18n.t('location_not_captured'), false);
  });

  // --- Submit handler ---------------------------------------------------------------

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    setFormNotice(null);

    const text = validateComplaint();
    if (text === null) return;

    setSubmitting(true);
    showStep(analysisStep);
    analysisOutcome.hidden = true;
    analysisHeading.textContent = window.CivicSyncI18n.t('analysis_heading');
    resetAnalysisIcon();
    analysisCaption.textContent = window.CivicSyncI18n.t('analysis_caption');

    const animation = runStageAnimation();
    const startedAt = Date.now();

    try {
      const issue = await CivicSyncApi.submitIssue(text, capturedLocation, window.CivicSyncI18n.getLanguage());
      const elapsed = Date.now() - startedAt;
      if (elapsed < MIN_ANALYSIS_DISPLAY_MS) {
        await wait(MIN_ANALYSIS_DISPLAY_MS - elapsed);
      }
      animation.finish();
      lastCreatedIssue = issue;
      renderAnalysisOutcome(issue);
    } catch (err) {
      animation.finish();
      showStep(formStep);
      const tone = err.type === 'validation' ? 'error' : 'error';
      setFormNotice(err.message, tone);
    } finally {
      setSubmitting(false);
    }
  });

  viewResultButton.addEventListener('click', () => {
    if (!lastCreatedIssue) return;
    renderResult(lastCreatedIssue);
    showStep(resultStep);
  });

  reportAnotherButton.addEventListener('click', () => {
    form.reset();
    setFieldError(null);
    setFormNotice(null);
    lastCreatedIssue = null;
    showStep(formStep);
  });

  // --- Real department list (no fabricated trust stats) -----------------------------

  async function loadDepartments() {
    if (!departmentListEl) return;
    try {
      const departments = await CivicSyncApi.getDepartments();
      departmentListEl.innerHTML = '';
      departments.forEach((dept) => {
        const li = document.createElement('li');
        li.className = 'department-list__item';
        li.textContent = dept.name;
        departmentListEl.appendChild(li);
      });
    } catch (err) {
      departmentListEl.innerHTML = '<li class="department-list__item">Department list unavailable right now.</li>';
    }
  }

  loadDepartments();
})();
