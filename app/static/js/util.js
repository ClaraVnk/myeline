// Myeline frontend util — chargé depuis base.html sur toutes les pages.
//
// Bug X11 (audit round 3) — centralise les helpers `escHtml` et
// `safeUrlJs` qui étaient dupliqués (en deux variantes incompatibles)
// à travers plusieurs templates : dashboard, scrapers, library,
// admin/dashboard, admin/enterprise_dashboard.
//
// Bug X4/X5/X9 (audit round 3) — `escHtml` strict (échappe aussi `'`)
// pour éviter qu'un titre/nom contenant `'` casse un attribut HTML
// ou un onclick template. `safeUrlJs` bloque les schemes
// `javascript:` / `data:` / `vbscript:` avant injection dans href.
//
// Tout nouveau template qui interpole du contenu user-controlled dans
// du HTML ou un href doit utiliser ces deux helpers (et JAMAIS les
// redéfinir localement).

(function (global) {
  "use strict";

  global.escHtml = function (s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  };

  global.safeUrlJs = function (raw) {
    if (typeof raw !== "string" || !raw) return "#";
    // Strip whitespace + control chars before scheme detection — certains
    // browsers tolèrent `java\tscript:...` et l'exécutent.
    var trimmed = raw.replace(/[\s\x00-\x1F]/g, "");
    if (/^https?:/i.test(trimmed) || /^mailto:/i.test(trimmed)) return raw;
    if (trimmed.startsWith("/") && !trimmed.startsWith("//")) return raw;
    if (trimmed.startsWith("#")) return raw;
    return "#";
  };

  // Bug X6 (audit round 3) — handler global pour les `<img class="js-hide-on-error">`,
  // équivalent du `onerror="this.style.display='none'"` inline mais
  // compatible CSP3 nonce-only. error event ne bubble pas par défaut,
  // on capture sur la fenêtre.
  document.addEventListener(
    "error",
    function (ev) {
      var t = ev.target;
      if (t && t.tagName === "IMG" && t.classList && t.classList.contains("js-hide-on-error")) {
        t.style.display = "none";
      }
    },
    true, // capture
  );

  // Bug X6 — handler global pour les `<select class="js-auto-submit">`,
  // équivalent du `onchange="this.form.submit()"` inline.
  document.addEventListener("change", function (ev) {
    var t = ev.target;
    if (t && t.classList && t.classList.contains("js-auto-submit") && t.form) {
      t.form.submit();
    }
  });
})(window);
