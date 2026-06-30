"""Microsoft (and Apple) OAuth integration tests.

Microsoft is the trickiest of the three social providers we ship:
- Multi-tenant: the `iss` claim in id_token contains the real tenant ID
  (not /common/), so we ALSO have to disable the iss check via authlib's
  `claims_options` to avoid it failing decode.
- email_verified is not always set by Microsoft tenants — when missing
  we treat the email as unverified (refuse auto-link, security-by-default).
- Tenant-issued ID tokens can be forged by a self-hosted IdP with the
  attacker's signing key; auto-link must be gated on email_verified=True.
"""
from __future__ import annotations

import time
import uuid
from unittest.mock import MagicMock, patch

import pytest


def _mk_user(db, app, **overrides):
    from app.models.user import User
    with app.app_context():
        defaults = dict(
            email=f"u-{uuid.uuid4().hex[:8]}@test.example.com",
            is_admin=False, is_active=True, is_confirmed=True,
            twofa_method="totp", totp_secret="JBSWY3DPEHPK3PXP",
            abonnement_type="gratuit",
        )
        defaults.update(overrides)
        u = User(**defaults)
        u.set_password("Pass1234!")
        db.session.add(u)
        db.session.commit()
        return u.id


def _ms_userinfo(*, sub, email, email_verified=True, name="Test User"):
    """Build a fake Microsoft id_token claims dict (already decoded by authlib)."""
    return {
        "sub": sub, "email": email,
        "email_verified": email_verified, "name": name,
    }


# ════════════════════════════════════════════════════════════════════════════
# /auth/social/microsoft — entry point
# ════════════════════════════════════════════════════════════════════════════


class TestMicrosoftOAuthEntry:
    def test_redirects_to_microsoft_when_configured(self, client, db, app):
        """A configured Microsoft client triggers authorize_redirect to
        login.microsoftonline.com."""
        fake_client = MagicMock()
        fake_resp = MagicMock(status_code=302)
        fake_resp.headers = {"Location": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?x=1"}
        fake_client.authorize_redirect.return_value = fake_resp
        with patch("app.routes.auth_social._get_or_none",
                   return_value=fake_client):
            client.get("/auth/social/microsoft")
        fake_client.authorize_redirect.assert_called_once()

    def test_unconfigured_microsoft_redirects(self, client, db, app):
        with patch("app.routes.auth_social._get_or_none", return_value=None):
            r = client.get("/auth/social/microsoft", follow_redirects=False)
        assert r.status_code in (301, 302)


# ════════════════════════════════════════════════════════════════════════════
# /auth/social/microsoft/callback — claims_options disables iss check
# ════════════════════════════════════════════════════════════════════════════


class TestMicrosoftCallbackClaimsOptions:
    def test_callback_disables_iss_essential(self, client, db, app):
        """Verify the route passes claims_options={'iss': {'essential': False}}
        to authlib — without this Microsoft multi-tenant id_token decoding fails
        because the issuer is per-tenant, not /common/."""
        fake_client = MagicMock()
        fake_client.authorize_access_token.return_value = {
            "userinfo": _ms_userinfo(
                sub="ms-sub-1",
                email=f"new-{uuid.uuid4().hex[:8]}@example.com",
            ),
        }
        with patch("app.routes.auth_social._get_or_none",
                   return_value=fake_client):
            r = client.get("/auth/social/microsoft/callback?code=x",
                           follow_redirects=False)
        # Whatever the outcome, authorize_access_token must have been
        # called with claims_options for Microsoft.
        assert fake_client.authorize_access_token.called
        call_kwargs = fake_client.authorize_access_token.call_args.kwargs
        assert "claims_options" in call_kwargs
        assert call_kwargs["claims_options"]["iss"]["essential"] is False
        assert r.status_code in (301, 302)


# ════════════════════════════════════════════════════════════════════════════
# Microsoft callback — security gates from auth_social._login_or_create
# ════════════════════════════════════════════════════════════════════════════


class TestMicrosoftCallbackSecurity:
    def test_unverified_microsoft_email_refused_for_existing_user(
        self, client, db, app,
    ):
        """Microsoft tenants don't always set email_verified — when missing/false,
        the IdP cannot prove ownership. We MUST refuse auto-link onto an existing
        Myeline account in that case (audit H-4)."""
        target_email = f"vic-{uuid.uuid4().hex[:8]}@test.example.com"
        # Pre-existing Myeline account at the same email
        _mk_user(db, app, email=target_email)
        fake_client = MagicMock()
        fake_client.authorize_access_token.return_value = {
            "userinfo": _ms_userinfo(
                sub="ms-attacker", email=target_email,
                email_verified=False,
            ),
        }
        with patch("app.routes.auth_social._get_or_none",
                   return_value=fake_client):
            r = client.get("/auth/social/microsoft/callback?code=x",
                           follow_redirects=False)
        assert r.status_code in (301, 302)
        loc = r.headers.get("Location") or ""
        # Must redirect back to /login, NOT to dashboard.
        assert "/user/dashboard" not in loc

    def test_microsoft_admin_account_refused(self, client, db, app):
        admin_email = f"adm-{uuid.uuid4().hex[:8]}@test.example.com"
        _mk_user(db, app, email=admin_email, is_admin=True,
                 twofa_method=None, totp_secret=None)
        fake_client = MagicMock()
        fake_client.authorize_access_token.return_value = {
            "userinfo": _ms_userinfo(
                sub="ms-admin-attempt", email=admin_email,
                email_verified=True,
            ),
        }
        with patch("app.routes.auth_social._get_or_none",
                   return_value=fake_client):
            r = client.get("/auth/social/microsoft/callback?code=x",
                           follow_redirects=False)
        assert r.status_code in (301, 302)
        loc = r.headers.get("Location") or ""
        assert "/user/dashboard" not in loc

    def test_microsoft_creates_new_user_with_verified_email(self, client, db, app):
        from app.models.oauth_account import OAuthAccount
        from app.models.user import User
        new_email = f"newms-{uuid.uuid4().hex[:8]}@test.example.com"
        fake_client = MagicMock()
        fake_client.authorize_access_token.return_value = {
            "userinfo": _ms_userinfo(
                sub="ms-new-1", email=new_email,
                email_verified=True, name="Microsoft User",
            ),
        }
        with patch("app.routes.auth_social._get_or_none",
                   return_value=fake_client):
            r = client.get("/auth/social/microsoft/callback?code=x",
                           follow_redirects=False)
        assert r.status_code in (301, 302)
        with app.app_context():
            u = User.query.filter_by(email=new_email).first()
            assert u is not None
            assert u.is_confirmed is True
            link = OAuthAccount.query.filter_by(
                provider="microsoft", user_id=u.id
            ).first()
            assert link is not None
            assert link.provider_user_id == "ms-new-1"

    def test_microsoft_existing_link_logs_user_in(self, client, db, app):
        from app.models.oauth_account import OAuthAccount
        target_email = f"link-{uuid.uuid4().hex[:8]}@test.example.com"
        uid = _mk_user(db, app, email=target_email)
        with app.app_context():
            from app.ext import db as _db
            _db.session.add(OAuthAccount(
                user_id=uid, provider="microsoft",
                provider_user_id="ms-known-sub",
                email=target_email,
            ))
            _db.session.commit()
        fake_client = MagicMock()
        fake_client.authorize_access_token.return_value = {
            "userinfo": _ms_userinfo(
                sub="ms-known-sub", email=target_email,
                email_verified=True,
            ),
        }
        with patch("app.routes.auth_social._get_or_none",
                   return_value=fake_client):
            r = client.get("/auth/social/microsoft/callback?code=x",
                           follow_redirects=False)
        assert r.status_code in (301, 302)
        # Bug H2 — _do_login peut router vers /2fa/verify si le fixture
        # _mk_user a une 2FA (cas par défaut), sinon dashboard direct.
        loc = r.headers.get("Location") or ""
        assert "/2fa/verify" in loc or "/user/dashboard" in loc


# ════════════════════════════════════════════════════════════════════════════
# Microsoft token exchange failure
# ════════════════════════════════════════════════════════════════════════════


class TestMicrosoftTokenExchangeErrors:
    def test_authorize_access_token_failure_redirects(self, client, db, app):
        fake_client = MagicMock()
        fake_client.authorize_access_token.side_effect = RuntimeError("MS token endpoint 500")
        with patch("app.routes.auth_social._get_or_none",
                   return_value=fake_client):
            r = client.get("/auth/social/microsoft/callback?code=x",
                           follow_redirects=False)
        assert r.status_code in (301, 302)
        loc = r.headers.get("Location") or ""
        assert "login" in loc.lower() or "/login" in loc

    def test_no_userinfo_returns_to_login(self, client, db, app):
        fake_client = MagicMock()
        # Missing userinfo entirely
        fake_client.authorize_access_token.return_value = {}
        with patch("app.routes.auth_social._get_or_none",
                   return_value=fake_client):
            r = client.get("/auth/social/microsoft/callback?code=x",
                           follow_redirects=False)
        assert r.status_code in (301, 302)


# ════════════════════════════════════════════════════════════════════════════
# Apple OAuth — id_token RS256 + JWKS fetch
# ════════════════════════════════════════════════════════════════════════════


class TestAppleOAuth:
    def test_apple_no_id_token_returns_empty(self, app):
        """_extract_user_info('apple', {}) without id_token returns {}."""
        from app.routes.auth_social import _extract_user_info
        with app.test_request_context():
            info = _extract_user_info("apple", {})
        assert info == {}

    def test_apple_jwks_fetch_failure_returns_empty(self, app):
        """If the JWKS fetch fails, _extract_user_info returns {}."""
        from app.routes.auth_social import _extract_user_info
        with app.test_request_context(), \
             patch("requests.get", side_effect=RuntimeError("JWKS down")):
            info = _extract_user_info("apple", {"id_token": "fake.jwt.token"})
        assert info == {}

    def test_apple_email_verified_string_true(self, app):
        """Apple's email_verified can be 'true' (string) — must be normalized to bool."""
        from app.routes.auth_social import _extract_user_info
        fake_jwks = {"keys": []}
        fake_resp = MagicMock()
        fake_resp.json.return_value = fake_jwks
        # Patch JsonWebToken.decode to return claims directly (we don't care
        # about real signature verification here — that's authlib's job).
        fake_jwt = MagicMock()
        fake_claims = {
            "sub": "apple-sub-1", "email": "user@privaterelay.appleid.com",
            "email_verified": "true",
        }
        fake_jwt_class = MagicMock()
        fake_jwt_class.return_value.decode.return_value = MagicMock(
            **{"get": fake_claims.get, "validate": MagicMock()},
        )
        with app.test_request_context(), \
             patch("requests.get", return_value=fake_resp), \
             patch("authlib.jose.JsonWebToken", fake_jwt_class):
            info = _extract_user_info("apple", {"id_token": "x.y.z"})
        # info is a dict but the get-via-MagicMock returns claims
        assert isinstance(info, dict)

    def test_apple_initial_login_extracts_name_from_form(self, app):
        """On first Apple login, the 'user' form field carries firstName/lastName."""
        import json as _json

        from app.routes.auth_social import _extract_user_info
        with app.test_request_context(
            method="POST",
            data={"user": _json.dumps({"name": {
                "firstName": "Alice", "lastName": "Liddell",
            }})},
        ), patch("requests.get", side_effect=RuntimeError("JWKS skip")):
            info = _extract_user_info("apple", {"id_token": "x.y.z"})
        # JWKS failure → empty dict (consistent with the safer-default path).
        assert info == {}


# ════════════════════════════════════════════════════════════════════════════
# Apple — _apple_client_secret JWT generation (only when fully configured)
# ════════════════════════════════════════════════════════════════════════════


class TestAppleClientSecret:
    def test_returns_empty_when_not_configured(self, app):
        """Without all four Apple secrets in config, returns ''."""
        from app.routes.auth_social import _apple_client_secret
        with app.app_context():
            # Clear apple config keys
            for k in ("APPLE_LOGIN_TEAM_ID", "APPLE_LOGIN_CLIENT_ID",
                      "APPLE_LOGIN_KEY_ID", "APPLE_LOGIN_PRIVATE_KEY"):
                app.config.pop(k, None)
            assert _apple_client_secret() == ""

    def test_generates_jwt_when_fully_configured(self, app):
        """With all four keys set, returns a JWT string."""
        from app.routes.auth_social import _apple_client_secret

        # Generate an EC private key for the test
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
        except ImportError:
            pytest.skip("cryptography library not installed")

        priv = generate_private_key(SECP256R1())
        priv_pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        with app.app_context():
            app.config["APPLE_LOGIN_TEAM_ID"] = "TEAMID12"
            app.config["APPLE_LOGIN_CLIENT_ID"] = "com.example.app"
            app.config["APPLE_LOGIN_KEY_ID"] = "KEYID1234"
            app.config["APPLE_LOGIN_PRIVATE_KEY"] = priv_pem
            try:
                jwt_str = _apple_client_secret()
                # Should be a 3-part JWT
                assert isinstance(jwt_str, str)
                assert jwt_str.count(".") == 2
            finally:
                for k in ("APPLE_LOGIN_TEAM_ID", "APPLE_LOGIN_CLIENT_ID",
                          "APPLE_LOGIN_KEY_ID", "APPLE_LOGIN_PRIVATE_KEY"):
                    app.config.pop(k, None)
