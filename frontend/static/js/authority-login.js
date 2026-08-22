/**
 * Authority login page (authority-login.html), Milestone 16-A.
 *
 * On successful login, the server has already set the HttpOnly session
 * cookie -- this script only needs to navigate to /admin afterward. It
 * never touches the cookie, the password hash, or any secret directly.
 */
(function () {
  const { CivicSyncApi } = window;

  const form = document.getElementById('login-form');
  const usernameInput = document.getElementById('username-input');
  const passwordInput = document.getElementById('password-input');
  const noticeEl = document.getElementById('login-notice');
  const submitButton = document.getElementById('login-submit-button');
  const submitLabel = document.getElementById('login-submit-label');

  function setNotice(message) {
    if (!message) {
      noticeEl.hidden = true;
      noticeEl.textContent = '';
      return;
    }
    noticeEl.hidden = false;
    noticeEl.textContent = message;
  }

  function setSubmitting(isSubmitting) {
    submitButton.disabled = isSubmitting;
    submitLabel.textContent = isSubmitting ? 'Signing in\u2026' : 'Sign In';
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    setNotice(null);

    const username = usernameInput.value.trim();
    const password = passwordInput.value;
    if (!username || !password) {
      setNotice('Please enter both a username and password.');
      return;
    }

    setSubmitting(true);
    try {
      await CivicSyncApi.authorityLogin(username, password);
      window.location.href = '/admin';
    } catch (err) {
      setNotice(err.message || 'Invalid username or password.');
      setSubmitting(false);
    }
  });
})();
