"""Coverage for app/routes/auth_social.py — OAuth social login flow.

OAuth round-trips can't hit live providers from tests, so we mock the
Authlib client + token exchange. Goal: cover the dispatcher (provider
allowlist, unconfigured-provider fallback), the user-info extractor for
each provider shape, and the three branches of _login_or_create
(known account, email match, new user).
"""
import uuid
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────
# Provider allowlist
# ─────────────────────────────────────────────────────────────────

class TestSocialLoginDispatcher:
    def test_unknown_provider_404(self, client):
        r = client.get("/auth/social/facebook")
        assert r.status_code == 404

    def test_unconfigured_provider_redirects_to_login(self, client, app, monkeypatch):
        # Force GOOGLE_LOGIN_CLIENT_ID empty so _get_or_none returns None
        monkeypatch.setitem(app.config, "GOOGLE_LOGIN_CLIENT_ID", "")
        r = client.get("/auth/social/google", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in r.headers.get("Location", "")


class TestSocialCallbackDispatcher:
    def test_unknown_provider_404(self, client):
        r = client.get("/auth/social/yahoo/callback")
        assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────
# _get_or_none — provider config gating
# ─────────────────────────────────────────────────────────────────

class TestGetOrNone:
    def test_unknown_provider_returns_none(self, app):
        from app.routes.auth_social import _get_or_none
        with app.test_request_context():
            assert _get_or_none("yahoo") is None

    def test_google_unconfigured_returns_none(self, app):
        from app.routes.auth_social import _get_or_none
        with app.test_request_context():
            app.config["GOOGLE_LOGIN_CLIENT_ID"] = ""
            assert _get_or_none("google") is None

    def test_microsoft_unconfigured_returns_none(self, app):
        from app.routes.auth_social import _get_or_none
        with app.test_request_context():
            app.config["MICROSOFT_LOGIN_CLIENT_ID"] = ""
            assert _get_or_none("microsoft") is None

    def test_apple_unconfigured_returns_none(self, app):
        from app.routes.auth_social import _get_or_none
        with app.test_request_context():
            app.config["APPLE_LOGIN_CLIENT_ID"] = ""
            assert _get_or_none("apple") is None

    def test_google_configured_returns_oauth_client(self, app):
        # create_app() runs once for the session-scoped fixture and only
        # registers oauth.google when GOOGLE_LOGIN_CLIENT_ID is already in
        # the config at init time. Tests that set it after the fact must
        # register the client themselves, otherwise oauth.google raises
        # "No such client" inside _get_or_none.
        from app.ext import oauth
        from app.routes.auth_social import _get_or_none
        with app.test_request_context():
            app.config["GOOGLE_LOGIN_CLIENT_ID"] = "fake-id"
            if "google" not in oauth._registry:
                oauth.register(
                    name="google",
                    client_id="fake-id",
                    client_secret="fake-secret",
                    client_kwargs={"scope": "openid email profile"},
                )
            client = _get_or_none("google")
            assert client is not None


# ─────────────────────────────────────────────────────────────────
# _extract_user_info — provider-specific token shapes
# ─────────────────────────────────────────────────────────────────

class TestExtractUserInfo:
    def test_google_userinfo_shape(self, app):
        from app.routes.auth_social import _extract_user_info
        with app.test_request_context():
            token = {"userinfo": {
                "sub": "g-123", "email": "u@gmail.com", "name": "User One",
                "email_verified": True,
            }}
            info = _extract_user_info("google", token)
        assert info == {
            "sub": "g-123", "email": "u@gmail.com", "name": "User One",
            "email_verified": True,
        }

    def test_google_userinfo_unverified_email(self, app):
        # When the IdP claims email_verified=False (or omits it), the flag
        # propagates so _login_or_create can refuse to auto-link.
        from app.routes.auth_social import _extract_user_info
        with app.test_request_context():
            token = {"userinfo": {"sub": "g-x", "email": "u@gmail.com", "name": "U"}}
            info = _extract_user_info("google", token)
        assert info["email_verified"] is False

    def test_microsoft_userinfo_shape(self, app):
        from app.routes.auth_social import _extract_user_info
        with app.test_request_context():
            token = {"userinfo": {"sub": "ms-456", "email": "u@outlook.com", "name": "MS User"}}
            info = _extract_user_info("microsoft", token)
        assert info["sub"] == "ms-456"
        assert info["email"] == "u@outlook.com"

    def test_google_missing_userinfo_returns_empty_strings(self, app):
        from app.routes.auth_social import _extract_user_info
        with app.test_request_context():
            info = _extract_user_info("google", {})
        assert info == {"sub": "", "email": "", "name": "", "email_verified": False}

    def test_apple_no_id_token_returns_empty(self, app):
        from app.routes.auth_social import _extract_user_info
        with app.test_request_context():
            info = _extract_user_info("apple", {})
        assert info == {}

    def test_unknown_provider_returns_empty(self, app):
        from app.routes.auth_social import _extract_user_info
        with app.test_request_context():
            info = _extract_user_info("yahoo", {"id_token": "x"})
        assert info == {}

    def test_apple_decode_failure_returns_empty(self, app):
        from app.routes.auth_social import _extract_user_info
        with app.test_request_context():
            with patch("requests.get") as g:
                g.side_effect = Exception("network down")
                info = _extract_user_info("apple", {"id_token": "abc"})
        assert info == {}


# ─────────────────────────────────────────────────────────────────
# _login_or_create — three-branch identity reconciliation
# ─────────────────────────────────────────────────────────────────

class TestLoginOrCreate:
    def test_known_oauth_account_logs_in_user(self, app, client):
        """Branch 1: existing OAuthAccount → log in linked user."""
        from app.ext import db
        from app.models.oauth_account import OAuthAccount
        from app.models.user import User
        from app.routes.auth_social import _login_or_create

        with app.app_context():
            u = User(
                email=f"oauth-{uuid.uuid4().hex[:6]}@example.com",
                firstname="O", lastname="A",
                is_confirmed=True, is_active=True,
            )
            u.set_password("Pass1234!")
            db.session.add(u)
            db.session.flush()

            account = OAuthAccount(
                user_id=u.id, provider="google", provider_user_id="g-123",
                email=u.email,
            )
            db.session.add(account)
            db.session.commit()

            with client.application.test_request_context():
                resp = _login_or_create("google", {"sub": "g-123", "email": u.email, "name": ""})
            assert resp.status_code == 302
            assert "dashboard" in resp.headers.get("Location", "")

            db.session.delete(account)
            db.session.delete(u)
            db.session.commit()

    def test_email_match_links_account_and_logs_in(self, app, client):
        """Branch 2: no OAuth account but email matches → link + login.

        Requires email_verified=True from the IdP. Without it, audit H-4
        forces a refusal — covered by a separate test below.
        """
        from app.ext import db
        from app.models.oauth_account import OAuthAccount
        from app.models.user import User
        from app.routes.auth_social import _login_or_create

        with app.app_context():
            email = f"existing-{uuid.uuid4().hex[:6]}@example.com"
            u = User(
                email=email, firstname="E", lastname="X",
                is_confirmed=True, is_active=True,
            )
            u.set_password("Pass1234!")
            db.session.add(u)
            db.session.commit()
            user_id = u.id

            with client.application.test_request_context():
                resp = _login_or_create("google", {
                    "sub": "g-new-456", "email": email, "name": "",
                    "email_verified": True,
                })

            # Account row created, linked to existing user
            assert OAuthAccount.query.filter_by(user_id=user_id, provider="google").first() is not None
            assert resp.status_code == 302
            assert "dashboard" in resp.headers.get("Location", "")

            OAuthAccount.query.filter_by(user_id=user_id).delete()
            db.session.delete(u)
            db.session.commit()

    def test_email_match_refused_when_email_unverified(self, app, client):
        """Audit H-4: a self-hosted Microsoft tenant or any IdP that
        doesn't assert email_verified must NOT auto-link onto an existing
        Myeline account — that's the takeover vector we're closing.
        """
        from app.ext import db
        from app.models.oauth_account import OAuthAccount
        from app.models.user import User
        from app.routes.auth_social import _login_or_create

        with app.app_context():
            email = f"victim-{uuid.uuid4().hex[:6]}@example.com"
            u = User(
                email=email, firstname="V", lastname="X",
                is_confirmed=True, is_active=True,
            )
            u.set_password("Pass1234!")
            db.session.add(u)
            db.session.commit()
            user_id = u.id

            with client.application.test_request_context():
                resp = _login_or_create("google", {
                    "sub": "attacker-sub", "email": email, "name": "",
                    "email_verified": False,
                })

            # No account linked, redirect to login with a flash message.
            assert OAuthAccount.query.filter_by(user_id=user_id).first() is None
            assert resp.status_code == 302
            assert "/login" in resp.headers.get("Location", "")

            db.session.delete(u)
            db.session.commit()

    def test_admin_account_refuses_oauth_link(self, app, client):
        """Audit C-3 / H-4: even with a verified email, an admin row must
        never be auto-linked from social login — admins use /admin/login.
        """
        from app.ext import db
        from app.models.oauth_account import OAuthAccount
        from app.models.user import User
        from app.routes.auth_social import _login_or_create

        with app.app_context():
            email = f"admin-{uuid.uuid4().hex[:6]}@example.com"
            u = User(
                email=email, firstname="A", lastname="X",
                is_confirmed=True, is_active=True, is_admin=True,
            )
            u.set_password("Pass1234!")
            db.session.add(u)
            db.session.commit()
            user_id = u.id

            with client.application.test_request_context():
                resp = _login_or_create("google", {
                    "sub": "g-admin", "email": email, "name": "",
                    "email_verified": True,
                })

            assert OAuthAccount.query.filter_by(user_id=user_id).first() is None
            assert resp.status_code == 302
            assert "/login" in resp.headers.get("Location", "")

            db.session.delete(u)
            db.session.commit()

    def test_new_user_created_when_email_unknown(self, app, client):
        """Branch 3: no OAuth account, no email match → create user + account + login."""
        from app.ext import db
        from app.models.oauth_account import OAuthAccount
        from app.models.user import User
        from app.routes.auth_social import _login_or_create

        new_email = f"brand-new-{uuid.uuid4().hex[:6]}@example.com"
        with app.app_context():
            with client.application.test_request_context():
                resp = _login_or_create(
                    "google",
                    {"sub": "g-new-999", "email": new_email, "name": "Brand New"},
                )

            u = User.query.filter_by(email=new_email).first()
            assert u is not None
            assert u.is_confirmed is True
            assert u.firstname == "Brand"
            assert u.lastname == "New"
            assert OAuthAccount.query.filter_by(user_id=u.id).first() is not None
            assert resp.status_code in (302, 303)

            OAuthAccount.query.filter_by(user_id=u.id).delete()
            db.session.delete(u)
            db.session.commit()

    def test_apple_no_email_redirects_to_login(self, app, client):
        """Branch 3 with Apple: missing email is fatal — apple users must opt in to email sharing."""
        from app.routes.auth_social import _login_or_create

        with app.app_context():
            with client.application.test_request_context():
                resp = _login_or_create("apple", {"sub": "apple-noemail", "email": "", "name": ""})
            assert resp.status_code == 302
            assert "/login" in resp.headers.get("Location", "")


# ─────────────────────────────────────────────────────────────────
# _apple_client_secret
# ─────────────────────────────────────────────────────────────────

class TestAppleClientSecret:
    def test_returns_empty_when_unconfigured(self, app):
        from app.routes.auth_social import _apple_client_secret
        with app.test_request_context():
            app.config["APPLE_LOGIN_TEAM_ID"] = ""
            app.config["APPLE_LOGIN_CLIENT_ID"] = ""
            app.config["APPLE_LOGIN_KEY_ID"] = ""
            app.config["APPLE_LOGIN_PRIVATE_KEY"] = ""
            assert _apple_client_secret() == ""

    def test_returns_jwt_when_all_fields_set(self, app):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        from app.routes.auth_social import _apple_client_secret

        # Generate a real EC P-256 private key in PEM
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

        with app.test_request_context():
            app.config["APPLE_LOGIN_TEAM_ID"] = "TEAM123"
            app.config["APPLE_LOGIN_CLIENT_ID"] = "com.example.app"
            app.config["APPLE_LOGIN_KEY_ID"] = "KEY456"
            app.config["APPLE_LOGIN_PRIVATE_KEY"] = pem
            secret = _apple_client_secret()
        assert secret  # non-empty
        assert "." in secret  # JWT has dots
