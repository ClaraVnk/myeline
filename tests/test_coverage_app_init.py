"""Coverage push — app/__init__.py jinja filters + error handlers + restricts."""
from __future__ import annotations

import uuid

import pytest


def _mk_user(db, app, **overrides):
    from app.models.user import User
    with app.app_context():
        defaults = dict(
            email=f"u-{uuid.uuid4().hex[:8]}@test.local",
            is_admin=False, is_active=True, is_confirmed=True,
            twofa_method="totp", totp_secret="JBSWY3DPEHPK3PXP",
            abonnement_type="premium_pro",
        )
        defaults.update(overrides)
        u = User(**defaults)
        u.set_password("x")
        db.session.add(u)
        db.session.commit()
        return u.id


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


# ────────────────────────────────────────────────────────────────────
# Jinja filters
# ────────────────────────────────────────────────────────────────────

class TestEmailFormatFilter:
    def test_empty_input_returns_empty(self, app):
        with app.app_context():
            f = app.jinja_env.filters["email_format"]
            assert f("") == ""
            assert f(None) == ""

    def test_renders_unordered_list(self, app):
        with app.app_context():
            f = app.jinja_env.filters["email_format"]
            html = f("- one\n- two\n- three")
            assert "<ul>" in html
            assert "<li>one</li>" in html
            assert html.endswith("</ul>")

    def test_renders_paragraphs_with_breaks(self, app):
        with app.app_context():
            f = app.jinja_env.filters["email_format"]
            html = f("Hello\nworld")
            assert "Hello<br>" in html
            assert "world<br>" in html

    def test_mixes_lists_and_paragraphs(self, app):
        with app.app_context():
            f = app.jinja_env.filters["email_format"]
            html = f("Header\n- a\n- b\nFooter")
            assert "<ul>" in html
            assert "</ul>" in html
            assert "Header" in html
            assert "Footer" in html


class TestSafeUrlFilter:
    @pytest.mark.parametrize("dangerous", [
        "javascript:alert(1)",
        "JAVASCRIPT:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:alert(1)",
        "java\tscript:alert(1)",  # tab-injection bypass
        "//attacker.example/x",   # protocol-relative
    ])
    def test_blocks_dangerous_schemes(self, app, dangerous):
        with app.app_context():
            f = app.jinja_env.filters["safe_url"]
            assert f(dangerous) == "#"

    @pytest.mark.parametrize("ok", [
        "https://example.com/path",
        "http://example.com",
        "mailto:user@example.com",
        "/internal/path",
        "#anchor",
    ])
    def test_allows_safe_urls(self, app, ok):
        with app.app_context():
            f = app.jinja_env.filters["safe_url"]
            assert f(ok) == ok

    def test_handles_non_string(self, app):
        with app.app_context():
            f = app.jinja_env.filters["safe_url"]
            assert f(None) == "#"
            assert f(42) == "#"
            assert f("") == "#"


# ────────────────────────────────────────────────────────────────────
# Error handlers
# ────────────────────────────────────────────────────────────────────

class TestErrorHandlers:
    def test_404_html_renders_template(self, client):
        r = client.get("/no-such-route-zzz")
        assert r.status_code == 404
        assert b"<html" in r.data.lower() or b"<!doctype" in r.data.lower()

    def test_404_json_when_only_json_accepted(self, client):
        r = client.get("/no-such-route-zzz",
                       headers={"Accept": "application/json"})
        assert r.status_code == 404


# ────────────────────────────────────────────────────────────────────
# restrict_unvalidated_users — branches
# ────────────────────────────────────────────────────────────────────

class TestRestrictUnvalidated:
    def test_unconfirmed_user_redirected_from_protected_route(self, client, db, app):
        uid = _mk_user(db, app, is_confirmed=False, twofa_method=None,
                       totp_secret=None)
        _login(client, uid)
        r = client.get("/user/dashboard", follow_redirects=False)
        # Should redirect to settings (account_settings) per the route's logic.
        assert r.status_code in (301, 302)

    def test_confirmed_no_2fa_redirected_to_2fa_setup(self, client, db, app):
        uid = _mk_user(db, app, twofa_method=None, totp_secret=None)
        _login(client, uid)
        r = client.get("/user/dashboard", follow_redirects=False)
        loc = r.headers.get("Location") or ""
        assert r.status_code in (301, 302)
        assert "/2fa/setup" in loc or "2fa" in loc

    def test_2fa_bypass_user_can_reach_dashboard(self, client, db, app, monkeypatch):
        bypass_email = f"oauth-{uuid.uuid4().hex[:8]}@test.local"
        monkeypatch.setenv("BYPASS_2FA_EMAILS", bypass_email)
        uid = _mk_user(db, app, email=bypass_email,
                       twofa_method=None, totp_secret=None)
        _login(client, uid)
        r = client.get("/user/dashboard", follow_redirects=False)
        # Either 200 (rendered) or redirect to e.g. /payment/resubscribe
        # — but NOT to /2fa/setup.
        loc = r.headers.get("Location") or ""
        assert "/2fa" not in loc

    def test_confirmed_free_user_not_gated(self, client, db, app):
        # OSS single-user build : un compte gratuit confirmé & actif (2FA OK)
        # a un accès complet. Le gate restrict_unvalidated_users ne le
        # redirige plus — il atteint le dashboard (200).
        uid = _mk_user(db, app, abonnement_type="gratuit",
                       ai_provider="mistral", ai_api_key_enc="enc")
        _login(client, uid)
        r = client.get("/user/dashboard", follow_redirects=False)
        assert r.status_code == 200

    def test_admin_bypasses_all_gates(self, client, db, app):
        uid = _mk_user(db, app, is_admin=True, is_confirmed=True,
                       abonnement_type="gratuit")
        _login(client, uid)
        # Admin should not be redirected by restrict_unvalidated_users —
        # the admin login flow is the gate. But /user/dashboard isn't
        # admin-restricted; we just check the gate doesn't fire.
        r = client.get("/admin/dashboard", follow_redirects=False)
        # Either 200 (admin page renders) or 302 to admin login — never
        # to /payment/resubscribe.
        loc = r.headers.get("Location") or ""
        assert "/payment/resubscribe" not in loc
