/**
 * CivicSync theme & accessibility preferences (Milestone 25.9).
 *
 * Deliberately loaded in <head>, before body content paints, so the
 * correct theme applies immediately -- no flash of the wrong theme.
 * Everything here is genuine, working behavior, persisted in
 * localStorage: no fake/inert controls.
 *
 * Precedence for theme: explicit saved choice > OS prefers-color-scheme
 * (handled by the CSS media query in tokens.css, not here) > light.
 * This file only needs to *apply* an explicit saved choice, if one
 * exists; the CSS itself handles the "no explicit choice yet" case via
 * `@media (prefers-color-scheme: dark)`.
 */
(function () {
  var STORAGE_KEYS = {
    theme: 'civicsync:theme', // 'light' | 'dark'
    fontScale: 'civicsync:fontScale', // number, e.g. 1, 1.1, 1.2
    reducedMotion: 'civicsync:reducedMotion', // 'true' | 'false'
  };
  var FONT_SCALE_MIN = 0.875;
  var FONT_SCALE_MAX = 1.3;
  var FONT_SCALE_STEP = 0.075;

  function readStorage(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (err) {
      return null; // localStorage can throw in some private-browsing modes
    }
  }

  function writeStorage(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (err) {
      // Silently ignore -- the preference just won't persist this session.
    }
  }

  function applyTheme(theme) {
    var root = document.documentElement;
    if (theme === 'dark' || theme === 'light') {
      root.setAttribute('data-theme', theme);
      root.setAttribute('data-theme-explicit', 'true');
    } else {
      root.removeAttribute('data-theme');
      root.removeAttribute('data-theme-explicit');
    }
  }

  function getTheme() {
    var saved = readStorage(STORAGE_KEYS.theme);
    if (saved === 'dark' || saved === 'light') return saved;
    return null; // no explicit choice -- CSS media query decides
  }

  function setTheme(theme) {
    writeStorage(STORAGE_KEYS.theme, theme);
    applyTheme(theme);
    document.dispatchEvent(new CustomEvent('civicsync:themechange', { detail: { theme: theme } }));
  }

  function currentEffectiveTheme() {
    var explicit = getTheme();
    if (explicit) return explicit;
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches
      ? 'dark'
      : 'light';
  }

  function toggleTheme() {
    var next = currentEffectiveTheme() === 'dark' ? 'light' : 'dark';
    setTheme(next);
    return next;
  }

  function applyFontScale(scale) {
    document.documentElement.style.setProperty('--a11y-font-scale', String(scale));
  }

  function getFontScale() {
    var saved = parseFloat(readStorage(STORAGE_KEYS.fontScale));
    return Number.isFinite(saved) ? saved : 1;
  }

  function setFontScale(scale) {
    var clamped = Math.min(FONT_SCALE_MAX, Math.max(FONT_SCALE_MIN, scale));
    writeStorage(STORAGE_KEYS.fontScale, String(clamped));
    applyFontScale(clamped);
    return clamped;
  }

  function increaseFontScale() {
    return setFontScale(getFontScale() + FONT_SCALE_STEP);
  }

  function decreaseFontScale() {
    return setFontScale(getFontScale() - FONT_SCALE_STEP);
  }

  function resetFontScale() {
    return setFontScale(1);
  }

  function applyReducedMotion(enabled) {
    if (enabled) {
      document.documentElement.setAttribute('data-reduced-motion', 'true');
    } else {
      document.documentElement.removeAttribute('data-reduced-motion');
    }
  }

  function getReducedMotion() {
    return readStorage(STORAGE_KEYS.reducedMotion) === 'true';
  }

  function setReducedMotion(enabled) {
    writeStorage(STORAGE_KEYS.reducedMotion, enabled ? 'true' : 'false');
    applyReducedMotion(enabled);
  }

  // Apply saved preferences immediately (this script is loaded in
  // <head>, synchronously, before body content paints).
  var savedTheme = getTheme();
  if (savedTheme) applyTheme(savedTheme);
  applyFontScale(getFontScale());
  if (getReducedMotion()) applyReducedMotion(true);

  window.CivicSyncTheme = {
    getTheme: currentEffectiveTheme,
    setTheme: setTheme,
    toggleTheme: toggleTheme,
    getFontScale: getFontScale,
    setFontScale: setFontScale,
    increaseFontScale: increaseFontScale,
    decreaseFontScale: decreaseFontScale,
    resetFontScale: resetFontScale,
    getReducedMotion: getReducedMotion,
    setReducedMotion: setReducedMotion,
    FONT_SCALE_MIN: FONT_SCALE_MIN,
    FONT_SCALE_MAX: FONT_SCALE_MAX,
  };
})();
