/* FinMan idle timeout (client side).
   The server is the real gatekeeper (10 min, checked on every request).
   This script only makes it friendly: it keeps the server's clock fresh while
   the user is working on a page, warns at 9 minutes, and logs out at 10 even
   if the tab is just sitting there.
   Reads the token from <meta name="csrf-token"> in base.html. Add to base.html:
     <script src="{{ url_for('static', filename='js/idle_timeout.js') }}"></script>
   Include it on logged-in pages only (not login or reset-password). */
(function () {
  var IDLE_MS = 10 * 60 * 1000, WARN_MS = 9 * 60 * 1000, PING_EVERY = 60 * 1000;
  var lastActivity = Date.now(), lastPing = Date.now(), banner = null, done = false;

  function csrf() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.content : (window.FINMAN_CSRF || '');
  }
  function post(url) {
    return fetch(url, { method: 'POST', credentials: 'same-origin',
      headers: { 'X-CSRFToken': csrf() } });
  }
  function logout() {
    if (done) return; done = true;
    // redirect:'manual' stops fetch from following the redirect to /login, which
    // would render that page in the background and use up the flash message.
    fetch('/logout?reason=idle', { method: 'POST', credentials: 'same-origin', redirect: 'manual',
      headers: { 'X-CSRFToken': csrf() } })
      .finally(function () { window.location = '/login'; });
  }
  function hideBanner() { if (banner) { banner.remove(); banner = null; } }
  function showBanner() {
    if (banner) return;
    banner = document.createElement('div');
    banner.setAttribute('role', 'alertdialog');
    banner.style.cssText = 'position:fixed;left:50%;bottom:24px;transform:translateX(-50%);' +
      'background:#1f2937;color:#fff;padding:14px 18px;border-radius:10px;z-index:9999;' +
      'box-shadow:0 6px 24px rgba(0,0,0,.3);font:14px/1.4 system-ui,sans-serif;max-width:92vw';
    banner.innerHTML = 'You will be logged out in 1 minute because of inactivity. ' +
      '<button type="button" style="margin-left:12px;padding:6px 12px;border:0;border-radius:6px;' +
      'cursor:pointer;font-weight:600">Stay logged in</button>';
    banner.querySelector('button').addEventListener('click', activity);
    document.body.appendChild(banner);
  }
  function activity() {
    if (done) return;
    var now = Date.now();
    lastActivity = now; hideBanner();
    if (now - lastPing > PING_EVERY) {          // tell the server we are still here
      lastPing = now;
      post('/api/keepalive').then(function (r) { if (r.status === 401) logout(); })
                            .catch(function () {});
    }
  }
  ['mousedown', 'keydown', 'touchstart', 'scroll', 'input'].forEach(function (e) {
    window.addEventListener(e, activity, { passive: true, capture: true });
  });
  setInterval(function () {
    var idle = Date.now() - lastActivity;
    if (idle >= IDLE_MS) logout();
    else if (idle >= WARN_MS) showBanner();
  }, 5000);
})();