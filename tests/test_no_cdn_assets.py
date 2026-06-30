"""Regression guard: no template references a public CDN for CSS / JS
(audit round 4 — A4-L2 debt). Every third-party asset must live under
``app/static/vendor/`` so a deployment can run air-gapped and no
request carries user metadata to jsdelivr / fonts.googleapis.com /
fonts.gstatic.com.

The CSP is locked in parallel: Sovereign + on-prem responses must NOT
allow those origins in script-src / style-src / font-src.
"""
import os
import re


_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "templates")
_FORBIDDEN_ORIGINS = (
    "cdn.jsdelivr.net",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
)
# Only inspect the actual asset references (href / src). Comments and
# CSP fallback strings are skipped via a strict regex.
_HTML_REF_RE = re.compile(r"""(?:href|src)\s*=\s*["']([^"']+)["']""")


def _walk_templates():
    for root, _, files in os.walk(_TEMPLATES_DIR):
        for name in files:
            if name.endswith((".html", ".htm")):
                yield os.path.join(root, name)


def test_no_template_references_public_cdn():
    """No href= or src= attribute should point at jsdelivr / Google fonts."""
    offenders = []
    for path in _walk_templates():
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        for match in _HTML_REF_RE.findall(content):
            for forbidden in _FORBIDDEN_ORIGINS:
                if forbidden in match:
                    offenders.append((path, forbidden, match))
    assert not offenders, (
        "Templates still pulling assets from a public CDN — every third-party "
        "lib must live under app/static/vendor/. Offenders:\n"
        + "\n".join(f"  {p} → {f} ({m})" for p, f, m in offenders)
    )


def test_sovereign_csp_does_not_allow_cdn_origins(client, app):
    """A response served in Sovereign mode must NOT carry a CSP allowing
    the public CDN hosts."""
    with app.app_context():
        app.config["SOVEREIGN_MODE"] = True
        try:
            r = client.get("/")
            csp = r.headers.get("Content-Security-Policy", "")
        finally:
            app.config["SOVEREIGN_MODE"] = False
    for forbidden in _FORBIDDEN_ORIGINS:
        assert forbidden not in csp, (
            f"Sovereign CSP still mentions {forbidden!r}:\n{csp}"
        )


def test_vendor_libraries_are_present_on_disk():
    """Sanity: the files the templates reference must actually exist."""
    vendor = os.path.join(_TEMPLATES_DIR, "..", "static", "vendor")
    expected = [
        "bootstrap/bootstrap.min.css",
        "bootstrap/bootstrap.bundle.min.js",
        "bootstrap-icons/bootstrap-icons.css",
        "bootstrap-icons/fonts/bootstrap-icons.woff2",
        "bootstrap-icons/fonts/bootstrap-icons.woff",
        "chart/chart.umd.min.js",
        "quill/quill.snow.css",
        "quill/quill.min.js",
        "select2/select2.min.css",
        "select2/select2.min.js",
        "tom-select/tom-select.bootstrap5.min.css",
        "tom-select/tom-select.complete.min.js",
    ]
    missing = [
        p for p in expected
        if not os.path.isfile(os.path.join(vendor, p))
    ]
    assert not missing, f"Vendor files missing on disk: {missing}"
