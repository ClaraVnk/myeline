"""Coverage push — /login route branch coverage."""
from __future__ import annotations

import uuid
from unittest.mock import patch


def _mk_user(db, app, **overrides):
    from app.models.user import User
    with app.app_context():
        defaults = dict(
            email=f"l-{uuid.uuid4().hex[:8]}@test.example.com",
            is_admin=False, is_active=True, is_confirmed=True,
            twofa_method="totp", totp_secret="JBSWY3DPEHPK3PXP",
            abonnement_type="premium_pro",
        )
        defaults.update(overrides)
        u = User(**defaults)
        u.set_password("LoginPass1!")
        db.session.add(u)
        db.session.commit()
        return u.id


# ────────────────────────────────────────────────────────────────────
# /login — every branch
# ────────────────────────────────────────────────────────────────────

class TestLoginBranches:
    def test_recaptcha_failure(self, client, db, app):
        with patch("app.routes.auth.verify_recaptcha", return_value=False):
            r = client.post("/login",
                            data={"email": "x@y.local", "password": "Pass1!"})
        assert r.status_code == 200

    def test_unconfirmed_user_blocked(self, client, db, app):
        email = f"unc-{uuid.uuid4().hex[:8]}@test.example.com"
        _mk_user(db, app, email=email, is_confirmed=False)
        with patch("app.routes.auth.verify_recaptcha", return_value=True):
            r = client.post("/login",
                            data={"email": email, "password": "LoginPass1!", "submit": "Se connecter"})
        # Renders the resend-confirmation page (200), no redirect.
        assert r.status_code == 200

    def test_admin_redirect_to_admin_login(self, client, db, app):
        email = f"adm-{uuid.uuid4().hex[:8]}@test.example.com"
        _mk_user(db, app, email=email, is_admin=True, is_confirmed=True)
        with patch("app.routes.auth.verify_recaptcha", return_value=True):
            r = client.post("/login",
                            data={"email": email, "password": "LoginPass1!", "submit": "Se connecter"},
                            follow_redirects=False)
        assert r.status_code in (301, 302)
        assert "/admin/" in (r.headers.get("Location") or "")

    def test_2fa_bypass_user_skips_to_dashboard(self, client, db, app, monkeypatch):
        email = f"byp-{uuid.uuid4().hex[:8]}@test.example.com"
        monkeypatch.setenv("BYPASS_2FA_EMAILS", email)
        _mk_user(db, app, email=email, twofa_method=None, totp_secret=None)
        with patch("app.routes.auth.verify_recaptcha", return_value=True):
            r = client.post("/login",
                            data={"email": email, "password": "LoginPass1!", "submit": "Se connecter"},
                            follow_redirects=False)
        loc = r.headers.get("Location") or ""
        assert r.status_code in (301, 302)
        # 2FA bypassed → straight to the dashboard, never the 2FA flow.
        assert "/2fa" not in loc

    def test_user_with_2fa_redirected_to_verify(self, client, db, app):
        email = f"v-{uuid.uuid4().hex[:8]}@test.example.com"
        _mk_user(db, app, email=email)  # has totp by default
        with patch("app.routes.auth.verify_recaptcha", return_value=True):
            r = client.post("/login",
                            data={"email": email, "password": "LoginPass1!", "submit": "Se connecter"},
                            follow_redirects=False)
        assert r.status_code in (301, 302)
        assert "verify" in (r.headers.get("Location") or "").lower()

    def test_user_without_2fa_redirected_to_setup(self, client, db, app):
        email = f"s-{uuid.uuid4().hex[:8]}@test.example.com"
        _mk_user(db, app, email=email, twofa_method=None, totp_secret=None)
        with patch("app.routes.auth.verify_recaptcha", return_value=True):
            r = client.post("/login",
                            data={"email": email, "password": "LoginPass1!", "submit": "Se connecter"},
                            follow_redirects=False)
        assert r.status_code in (301, 302)
        assert "setup" in (r.headers.get("Location") or "").lower()

    def test_wrong_password_renders_login(self, client, db, app):
        email = f"wp-{uuid.uuid4().hex[:8]}@test.example.com"
        _mk_user(db, app, email=email)
        with patch("app.routes.auth.verify_recaptcha", return_value=True):
            r = client.post("/login",
                            data={"email": email, "password": "WrongPass!",
                                  "submit": "Se connecter"})
        assert r.status_code == 200
        assert b"invalides" in r.data.lower() or b"identifiants" in r.data.lower()

    def test_unknown_email_renders_login(self, client, db, app):
        with patch("app.routes.auth.verify_recaptcha", return_value=True):
            r = client.post("/login",
                            data={"email": "ghost@test.example.com",
                                  "password": "AnyPass1!",
                                  "submit": "Se connecter"})
        assert r.status_code == 200


# ────────────────────────────────────────────────────────────────────
# /resend-confirmation extra paths
# ────────────────────────────────────────────────────────────────────

class TestResendConfirmationExtras:
    def test_already_confirmed_user_just_says_thanks(self, client, db, app):
        email = f"rc-{uuid.uuid4().hex[:8]}@test.example.com"
        _mk_user(db, app, email=email, is_confirmed=True)
        with patch("app.routes.auth.verify_recaptcha", return_value=True), \
             patch("app.routes.auth.send_email_via_brevo_api"):
            r = client.post("/resend-confirmation", data={"email": email})
        # 200 with a generic flash either way (info disclosure protection).
        assert r.status_code in (200, 301, 302)

    def test_unknown_email_silent(self, client, db, app):
        with patch("app.routes.auth.verify_recaptcha", return_value=True), \
             patch("app.routes.auth.send_email_via_brevo_api") as mock_send:
            r = client.post("/resend-confirmation",
                            data={"email": "ghost@test.example.com"})
        assert r.status_code in (200, 301, 302)
        # No email sent for unknown account (info disclosure protection).
        mock_send.assert_not_called()
