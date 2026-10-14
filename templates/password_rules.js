/* Single source of truth for FinMan's password rules in the browser.
   Mirrors _PASSWORD_RULES in app.py -- change both together. */
(function () {
  var RULES = [
    { key: "length",  label: "At least 8 characters", test: function (p) { return p.length >= 8; } },
    { key: "upper",   label: "One uppercase letter",  test: function (p) { return /[A-Z]/.test(p); } },
    { key: "lower",   label: "One lowercase letter",  test: function (p) { return /[a-z]/.test(p); } },
    { key: "number",  label: "One number",            test: function (p) { return /[0-9]/.test(p); } },
    { key: "special", label: "One special character", test: function (p) { return /[^A-Za-z0-9]/.test(p); } }
  ];

  function check(pw) {
    var r = {};
    RULES.forEach(function (x) { r[x.key] = x.test(pw); });
    return r;
  }

  function isValid(pw) {
    return RULES.every(function (x) { return x.test(pw); });
  }

  /* Draws the live checklist into a <ul>. extra = optional [{key,label,ok}]. */
  function renderChecklist(listEl, pw, extra) {
    var items = RULES.map(function (x) { return { key: x.key, label: x.label, ok: x.test(pw) }; })
                     .concat(extra || []);
    items.forEach(function (it) {
      var li = listEl.querySelector('[data-key="' + it.key + '"]');
      if (!li) {
        li = document.createElement("li");
        li.setAttribute("data-key", it.key);
        listEl.appendChild(li);
      }
      li.style.color = it.ok ? "var(--green, #16A34A)" : "var(--text-muted)";
      li.textContent = (it.ok ? "● " : "○ ") + it.label;
    });
  }

  window.PasswordRules = { rules: RULES, check: check, isValid: isValid, renderChecklist: renderChecklist };
})();
