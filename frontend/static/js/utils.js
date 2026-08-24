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

// --- Citizen header: mobile nav drawer (Milestone 25.9) ----------------------
// Shared by index.html and track.html. Desktop is unaffected -- the
// toggle button is display:none there and the nav/language/theme/
// Authority Portal panel is always visible inline.
(function initCitizenHeaderToggle() {
  const toggle = document.getElementById('citizen-nav-toggle');
  const headerInner = document.querySelector('.site-header__inner');
  const backdrop = document.getElementById('citizen-nav-backdrop');
  const menuIcon = toggle ? toggle.querySelector('.icon--menu') : null;
  const closeIcon = toggle ? toggle.querySelector('.icon--close') : null;
  if (!toggle || !headerInner) return;

  function isMobileDrawerActive() {
    // The drawer/backdrop pattern only applies below the CSS
    // breakpoint where the toggle is actually visible -- on desktop
    // the same markup stays inline and open() should never engage
    // backdrop/scroll-lock behavior.
    return window.getComputedStyle(toggle).display !== 'none';
  }

  function openDrawer() {
    headerInner.classList.add('is-nav-open');
    toggle.setAttribute('aria-expanded', 'true');
    toggle.setAttribute('aria-label', 'Close menu');
    if (menuIcon) menuIcon.hidden = true;
    if (closeIcon) closeIcon.hidden = false;
    if (backdrop && isMobileDrawerActive()) {
      backdrop.hidden = false;
      document.body.style.overflow = 'hidden';
    }
  }

  function closeDrawer() {
    headerInner.classList.remove('is-nav-open');
    toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-label', 'Open menu');
    if (menuIcon) menuIcon.hidden = false;
    if (closeIcon) closeIcon.hidden = true;
    if (backdrop) {
      backdrop.hidden = true;
      document.body.style.overflow = '';
    }
  }

  toggle.addEventListener('click', () => {
    if (headerInner.classList.contains('is-nav-open')) closeDrawer();
    else openDrawer();
  });

  if (backdrop) {
    backdrop.addEventListener('click', closeDrawer);
  }

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && headerInner.classList.contains('is-nav-open')) closeDrawer();
  });

  // Close the drawer after choosing a nav link or a language option
  // (but not the language dropdown's own trigger button, which must
  // stay open when clicked -- handled by its own module below).
  headerInner.querySelectorAll('.site-nav a, [data-language-option]').forEach((el) => {
    el.addEventListener('click', closeDrawer);
  });
})();

// --- Citizen header: language dropdown (Milestone 25.9) ----------------------
// Replaces the old three-separate-buttons switcher with a single
// trigger + popover menu. Reuses the existing [data-language-option]
// click-wiring and civicsync:languagechange event from i18n.js
// entirely unchanged -- this module only manages open/close state and
// keeps the trigger's own label in sync.
(function initLanguageDropdown() {
  const dropdown = document.getElementById('lang-dropdown');
  const trigger = document.getElementById('lang-dropdown-trigger');
  const menu = document.getElementById('lang-dropdown-menu');
  const currentLabel = document.getElementById('lang-dropdown-current');
  if (!dropdown || !trigger || !menu || !currentLabel) return;

  const LANG_KEYS = { en: 'lang_name_en', hi: 'lang_name_hi', bn: 'lang_name_bn' };

  function open() {
    menu.hidden = false;
    trigger.setAttribute('aria-expanded', 'true');
  }
  function close() {
    menu.hidden = true;
    trigger.setAttribute('aria-expanded', 'false');
  }

  trigger.addEventListener('click', (e) => {
    e.stopPropagation();
    if (menu.hidden) open();
    else close();
  });

  document.addEventListener('click', (e) => {
    if (!dropdown.contains(e.target)) close();
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !menu.hidden) {
      close();
      trigger.focus();
    }
  });

  function syncLabel() {
    if (!window.CivicSyncI18n) return;
    const lang = window.CivicSyncI18n.getLanguage();
    const key = LANG_KEYS[lang] || LANG_KEYS.en;
    currentLabel.textContent = window.CivicSyncI18n.t(key);
    menu.querySelectorAll('[data-language-option]').forEach((btn) => {
      const isActive = btn.getAttribute('data-language-option') === lang;
      btn.setAttribute('aria-checked', isActive ? 'true' : 'false');
      btn.classList.toggle('is-active', isActive);
    });
  }

  document.addEventListener('civicsync:languagechange', () => {
    syncLabel();
    close();
  });
  // Initial sync once i18n.js has applied translations on load.
  syncLabel();
})();

// --- Citizen header: theme toggle (Milestone 25.9) ---------------------------
(function initThemeToggle() {
  const button = document.getElementById('theme-toggle');
  if (!button || !window.CivicSyncTheme) return;
  const sunIcon = button.querySelector('.icon--sun');
  const moonIcon = button.querySelector('.icon--moon');

  function syncButton() {
    const isDark = window.CivicSyncTheme.getTheme() === 'dark';
    button.setAttribute('aria-pressed', isDark ? 'true' : 'false');
    button.setAttribute('aria-label', isDark ? 'Switch to light mode' : 'Switch to dark mode');
    if (sunIcon) sunIcon.hidden = isDark;
    if (moonIcon) moonIcon.hidden = !isDark;
  }

  button.addEventListener('click', () => {
    window.CivicSyncTheme.toggleTheme();
    syncButton();
  });

  document.addEventListener('civicsync:themechange', syncButton);

  syncButton();
})();


// --- Floating accessibility widget (Milestone 25.9) ---------------------------
// Every control here genuinely affects the page via CivicSyncTheme
// (theme.js) and persists in localStorage -- no decorative controls.
(function initAccessibilityWidget() {
  const toggle = document.getElementById('a11y-widget-toggle');
  const panel = document.getElementById('a11y-widget-panel');
  const widget = document.getElementById('a11y-widget');
  if (!toggle || !panel || !widget || !window.CivicSyncTheme) return;

  const fontDecrease = document.getElementById('a11y-font-decrease');
  const fontIncrease = document.getElementById('a11y-font-increase');
  const fontReset = document.getElementById('a11y-font-reset');
  const darkToggle = document.getElementById('a11y-dark-toggle');
  const motionToggle = document.getElementById('a11y-motion-toggle');

  function open() {
    panel.hidden = false;
    toggle.setAttribute('aria-expanded', 'true');
  }
  function close() {
    panel.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
  }

  toggle.addEventListener('click', (e) => {
    e.stopPropagation();
    if (panel.hidden) open();
    else close();
  });
  document.addEventListener('click', (e) => {
    if (!widget.contains(e.target)) close();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !panel.hidden) {
      close();
      toggle.focus();
    }
  });

  if (fontDecrease) fontDecrease.addEventListener('click', () => window.CivicSyncTheme.decreaseFontScale());
  if (fontIncrease) fontIncrease.addEventListener('click', () => window.CivicSyncTheme.increaseFontScale());
  if (fontReset) fontReset.addEventListener('click', () => window.CivicSyncTheme.resetFontScale());

  if (darkToggle) {
    darkToggle.checked = window.CivicSyncTheme.getTheme() === 'dark';
    darkToggle.addEventListener('change', () => {
      window.CivicSyncTheme.setTheme(darkToggle.checked ? 'dark' : 'light');
    });
    document.addEventListener('civicsync:themechange', (e) => {
      darkToggle.checked = e.detail.theme === 'dark';
    });
  }

  if (motionToggle) {
    motionToggle.checked = window.CivicSyncTheme.getReducedMotion();
    motionToggle.addEventListener('change', () => {
      window.CivicSyncTheme.setReducedMotion(motionToggle.checked);
    });
  }
})();

// --- Floating widget collision avoidance (Milestone 25.9 revision) ------------
// On narrow viewports, continuously checks whether either floating
// widget's actual on-screen position overlaps a real form control,
// button, label, or link anywhere on the page -- not just at the
// initial scroll position, but for any scroll position the citizen
// reaches. A widget that would overlap something is faded out and
// made non-interactive (pointer-events: none) for as long as the
// collision holds, and restored the instant it's clear again. This
// directly enforces "never covers meaningful content", rather than
// relying on hand-tuned per-page spacing that breaks the moment page
// content changes.
(function initFloatingWidgetCollisionAvoidance() {
  const a11yWidget = document.getElementById('a11y-widget');
  const contactWidget = document.querySelector('.contact-widget');
  if (!a11yWidget && !contactWidget) return;

  const PROTECTED_SELECTOR =
    'input, textarea, select, button, a[href], label, dt, dd, .field-hint, h1, h2, h3, p';
  let ticking = false;

  function rectsOverlap(a, b) {
    return !(a.right < b.left || b.right < a.left || a.bottom < b.top || b.bottom < a.top);
  }

  function widgetWouldCollide(widgetEl) {
    if (!widgetEl) return false;
    const widgetRect = widgetEl.getBoundingClientRect();
    if (widgetRect.width === 0 || widgetRect.height === 0) return false;
    const candidates = document.querySelectorAll(PROTECTED_SELECTOR);
    for (let i = 0; i < candidates.length; i++) {
      const el = candidates[i];
      if (el.closest('#a11y-widget') || el.closest('.contact-widget')) continue;
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      // Only elements currently within the viewport can visually collide.
      if (r.bottom < 0 || r.top > window.innerHeight) continue;
      if (rectsOverlap(widgetRect, r)) return true;
    }
    return false;
  }

  function applyState(widgetEl, hidden) {
    if (!widgetEl) return;
    widgetEl.classList.toggle('is-collision-hidden', hidden);
  }

  function update() {
    ticking = false;
    // Only meaningful below the desktop breakpoint where these widgets
    // are dense enough relative to page content to ever collide.
    if (window.innerWidth > 480) {
      applyState(a11yWidget, false);
      applyState(contactWidget, false);
      return;
    }
    applyState(a11yWidget, widgetWouldCollide(a11yWidget));
    applyState(contactWidget, widgetWouldCollide(contactWidget));
  }

  function requestUpdate() {
    if (ticking) return;
    ticking = true;
    window.requestAnimationFrame(update);
  }

  window.addEventListener('scroll', requestUpdate, { passive: true });
  window.addEventListener('resize', requestUpdate);

  // Re-check whenever the page's content actually changes -- several
  // pages populate real content asynchronously after a fetch (e.g. the
  // tracking page's issue details), which can appear or resize well
  // after this script's initial synchronous run and after the last
  // scroll/resize event, so scroll/resize alone isn't sufficient.
  const observer = new MutationObserver(requestUpdate);
  observer.observe(document.body, { childList: true, subtree: true, characterData: true });

  update();
})();
