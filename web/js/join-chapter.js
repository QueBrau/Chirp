/* join-chapter bounce page.
 *
 * SECURITY: `code` comes straight from the query string, i.e. it is
 * attacker-controlled. It is rendered with textContent ONLY (never
 * innerHTML/insertAdjacentHTML), and it is encodeURIComponent'd before it is
 * ever placed into the chirp:// URL. Validation below is cosmetic (a loose
 * shape check to show a friendly warning) — it never gates whether the code
 * is displayed or linked, since a malformed code can never become
 * script by going through textContent + encodeURIComponent.
 */
(function () {
  "use strict";

  var params = new URLSearchParams(window.location.search);
  var rawCode = params.get("code");
  var code = rawCode ? rawCode.trim() : "";

  var elHasCode = document.getElementById("state-has-code");
  var elNoCode = document.getElementById("state-no-code");
  var codeText = document.getElementById("code-text");
  var codeWarning = document.getElementById("code-warning");
  var openBtn = document.getElementById("open-chirp-btn");
  var openBtnNoCode = document.getElementById("open-chirp-btn-no-code");

  var BASE_SCHEME = "chirp://join-chapter";

  function buildDeepLink(rawValue) {
    if (!rawValue) return "chirp://join-chapter";
    return BASE_SCHEME + "?code=" + encodeURIComponent(rawValue);
  }

  function looksWellFormed(value) {
    // Backend mints secrets.token_urlsafe(9): URL-safe base64, no padding.
    // This is a loose sanity check only, not a security boundary.
    return /^[A-Za-z0-9_-]{6,64}$/.test(value);
  }

  if (code.length > 0) {
    elHasCode.hidden = false;
    elNoCode.hidden = true;

    // textContent only — never render attacker-controlled input as HTML.
    codeText.textContent = code;

    if (!looksWellFormed(code)) {
      codeWarning.hidden = false;
    }

    var deepLink = buildDeepLink(code);
    openBtn.setAttribute("href", deepLink);

    // Attempt the automatic hand-off. This silently does nothing if Chirp
    // isn't installed, which is exactly why the manual button above always
    // renders too.
    window.setTimeout(function () {
      window.location.href = deepLink;
    }, 50);
  } else {
    elHasCode.hidden = true;
    elNoCode.hidden = false;
    openBtnNoCode.setAttribute("href", "chirp://join-chapter");
  }
})();
