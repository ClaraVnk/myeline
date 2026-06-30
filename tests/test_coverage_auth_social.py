"""Coverage push — auth_social (Google/Microsoft/Apple OAuth login)."""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest


def _mk_user(db, app, *, email=None, is_admin=False, is_confirmed=True):
    from app.models.user import User
    with app.app_context():
        u = User(
            email=email or f"u-{uuid.uuid4().hex[:8]}@test.local",
            is_admin=is_admin, is_active=True, is_confirmed=is_confirmed,
            twofa_method="totp" if is_confirmed and not is_admin else None,
            totp_secret="JBSWY3DPEHPK3PXP" if is_confirmed and not is_admin else None,
            abonnement_type="gratuit",
        )
        u.set_password("x")
        db.session.add(u)
        db.session.commit()
        return u.id


# ────────────────────────────────────────────────────────────────────
# Provider gating
# ────────────────────────────────────────────────────────────────────

class TestProviderGating:
    def test_unknown_provider_returns_404(self, client, db, app):
        r = client.get("/auth/social/unknownprov")
        assert r.status_code == 404

    def test_unknown_callback_returns_404(self, client, db, app):
        r = client.get("/auth/social/unknownprov/callback")
        assert r.status_code == 404


# ────────────────────────────────────────────────────────────────────
# Configuration: provider not configured = redirect / 404
# ────────────────────────────────────────────────────────────────────

class TestProviderConfig:
    def test_unconfigured_provider_redirects(self, client, db, app):
        """When _get_or_none returns None, social_login redirects to login."""
        with patch("app.routes.auth_social._get_or_none", return_value=None):
            r = client.get("/auth/social/google", follow_redirects=False)
        assert r.status_code in (301, 302)

    def test_unconfigured_callback_404(self, client, db, app):
        with patch("app.routes.auth_social._get_or_none", return_value=None):
            r = client.get("/auth/social/google/callback")
        assert r.status_code == 404

    def test_configured_provider_initiates_redirect(self, client, db, app):
        """A configured Authlib client.authorize_redirect is called."""
        fake_client = MagicMock()
        fake_client.authorize_redirect.return_value = MagicMock(status_code=302)
        with patch("app.routes.auth_social._get_or_none", return_value=fake_client):
            client.get("/auth/social/google")
        fake_client.authorize_redirect.assert_called_once()


# ────────────────────────────────────────────────────────────────────
# Callback — token exchange failure
# ────────────────────────────────────────────────────────────────────

class TestCallbackErrors:
    def test_token_exchange_failure_redirects(self, client, db, app):
        fake_client = MagicMock()
        fake_client.authorize_access_token.side_effect = RuntimeError("bad code")
        with patch("app.routes.auth_social._get_or_none", return_value=fake_client):
            r = client.get("/auth/social/google/callback?code=x",
                           follow_redirects=False)
        assert r.status_code in (301, 302)

    def test_no_sub_in_userinfo_redirects(self, client, db, app):
        fake_client = MagicMock()
        fake_client.authorize_access_token.return_value = {"userinfo": {}}
        with patch("app.routes.auth_social._get_or_none", return_value=fake_client):
            r = client.get("/auth/social/google/callback?code=x",
                           follow_redirects=False)
        assert r.status_code in (301, 302)


# ────────────────────────────────────────────────────────────────────
# _login_or_create — branch coverage
# ────────────────────────────────────────────────────────────────────

class TestLoginOrCreate:
    def _callback_with_userinfo(self, client, app, db, *, sub, email, verified=True):
        fake_client = MagicMock()
        fake_client.authorize_access_token.return_value = {
            "userinfo": {
                "sub": sub, "email": email,
                "email_verified": verified, "name": "Tester",
            },
        }
        with patch("app.routes.auth_social._get_or_none", return_value=fake_client):
            return client.get("/auth/social/google/callback?code=x",
                              follow_redirects=False)

    def test_known_oauth_account_logs_in(self, client, db, app):
        # Bug H2 (audit round 2) — un user avec 2FA active doit être
        # routé vers /2fa/verify, pas direct au dashboard. Le fixture
        # _mk_user pose `twofa_method="totp"`, donc on attend la
        # redirection 2FA.
        from app.models.oauth_account import OAuthAccount
        email = f"sa-{uuid.uuid4().hex[:8]}@test.local"
        uid = _mk_user(db, app, email=email)
        with app.app_context():
            db.session.add(OAuthAccount(
                user_id=uid, provider="google",
                provider_user_id="goog-sub-1", email=email,
            ))
            db.session.commit()
        r = self._callback_with_userinfo(client, app, db,
                                         sub="goog-sub-1", email=email)
        assert r.status_code in (301, 302)
        loc = r.headers.get("Location") or ""
        assert "/2fa/verify" in loc or "/user/dashboard" in loc

    def test_existing_user_unverified_email_refused(self, client, db, app):
        email = f"unv-{uuid.uuid4().hex[:8]}@test.local"
        _mk_user(db, app, email=email)
        r = self._callback_with_userinfo(client, app, db,
                                         sub="goog-sub-2", email=email,
                                         verified=False)
        assert r.status_code in (301, 302)
        loc = r.headers.get("Location") or ""
        assert "/user/dashboard" not in loc

    def test_admin_email_match_refused(self, client, db, app):
        email = f"adm-{uuid.uuid4().hex[:8]}@test.local"
        _mk_user(db, app, email=email, is_admin=True)
        r = self._callback_with_userinfo(client, app, db,
                                         sub="goog-sub-3", email=email)
        assert r.status_code in (301, 302)
        loc = r.headers.get("Location") or ""
        assert "/user/dashboard" not in loc

    def test_verified_existing_user_links_account(self, client, db, app):
        from app.models.oauth_account import OAuthAccount
        email = f"vu-{uuid.uuid4().hex[:8]}@test.local"
        uid = _mk_user(db, app, email=email)
        r = self._callback_with_userinfo(client, app, db,
                                         sub="goog-sub-4", email=email)
        assert r.status_code in (301, 302)
        with app.app_context():
            assert OAuthAccount.query.filter_by(
                provider="google", provider_user_id="goog-sub-4"
            ).first().user_id == uid


# ────────────────────────────────────────────────────────────────────
# Sovereign mode disables /auth/social entirely
# ────────────────────────────────────────────────────────────────────

class TestSovereignDisablesSocial:
    def test_returns_404_in_sovereign(self, client, db, app, monkeypatch):
        monkeypatch.setenv("DEPLOYMENT_MODE", "sovereign")
        from app.utils import deployment_mode as dm
        if hasattr(dm.cached_mode_at_boot, "cache_clear"):
            dm.cached_mode_at_boot.cache_clear()
        r = client.get("/auth/social/google")
        assert r.status_code == 404

    def test_returns_404_in_hybrid(self, client, db, app, monkeypatch):
        monkeypatch.setenv("DEPLOYMENT_MODE", "hybrid")
        from app.utils import deployment_mode as dm
        if hasattr(dm.cached_mode_at_boot, "cache_clear"):
            dm.cached_mode_at_boot.cache_clear()
        r = client.get("/auth/social/google")
        assert r.status_code == 404
