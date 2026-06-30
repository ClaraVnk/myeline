"""Coverage for app/routes/user_cloud.py — cloud connection routes.

Strategy: split tests by what they need.

  - Pure helpers (_redirect_uri, _store_oauth_state, _pop_oauth_state,
    _connection_limit_reached, _get_service): unit tests with monkeypatch.
  - Auth gates: anonymous → 302/401 (no login flow needed).
  - Logic with logged-in users: a single Pro user fixture + targeted
    tests on the highest-traffic routes (list, disconnect, sync,
    set-folder, set-frequency).

Login uses the existing tests/helpers.login() helper. We mock all
external services (gdrive, dropbox, etc.) so HTTP calls don't fire.
"""
import uuid
from unittest.mock import MagicMock, patch

import pytest

from tests.helpers import login


# ─────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────

@pytest.fixture
def pro_user(app, db):
    """A Pro subscriber with 2FA disabled for fast logins."""
    from app.models.user import User
    with app.app_context():
        u = User(
            email=f"pro-cloud-{uuid.uuid4().hex[:6]}@example.com",
            firstname="P", lastname="C",
            is_confirmed=True, is_active=True,
            abonnement_type="premium_pro",
            twofa_method="email",
        )
        u.set_password("ProUser1!")
        db.session.add(u)
        db.session.commit()
        user_id = u.id
    yield user_id
    with app.app_context():
        from app.models.cloud_connection import CloudConnection
        CloudConnection.query.filter_by(user_id=user_id).delete()
        u = db.session.get(User, user_id)
        if u:
            db.session.delete(u)
        db.session.commit()


@pytest.fixture
def non_subscriber_user(app, db):
    # Pivot Free/Pro/Enterprise: a confirmed + active Free account is now a
    # base subscriber, so to exercise the _require_subscriber redirect we use
    # an UNCONFIRMED account (is_subscriber is False).
    from app.models.user import User
    with app.app_context():
        u = User(
            email=f"std-cloud-{uuid.uuid4().hex[:6]}@example.com",
            firstname="S", lastname="C",
            is_confirmed=False, is_active=True,
            abonnement_type="gratuit",
            twofa_method="email",
        )
        u.set_password("StdUser1!")
        db.session.add(u)
        db.session.commit()
        user_id = u.id
    yield user_id
    with app.app_context():
        from app.models.cloud_connection import CloudConnection
        CloudConnection.query.filter_by(user_id=user_id).delete()
        u = db.session.get(User, user_id)
        if u:
            db.session.delete(u)
        db.session.commit()


def _login(client, app, db, user_id, password):
    """Login by user ID; we look up the email since the user is detached."""
    from app.models.user import User
    with app.app_context():
        u = db.session.get(User, user_id)
        email = u.email
    return login(client, email, password)


# ─────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────

class TestRedirectUri:
    def test_uses_oauth_redirect_base_url_when_set(self, app):
        from app.routes.user_cloud import _redirect_uri
        with app.test_request_context():
            app.config["OAUTH_REDIRECT_BASE_URL"] = "https://myeline.io"
            uri = _redirect_uri("user_cloud.cloud_callback", provider="gdrive")
        assert uri.startswith("https://myeline.io")
        assert "/user/cloud/gdrive/callback" in uri

    def test_falls_back_to_external_url_without_base(self, app):
        from app.routes.user_cloud import _redirect_uri
        with app.test_request_context():
            app.config["OAUTH_REDIRECT_BASE_URL"] = ""
            uri = _redirect_uri("user_cloud.cloud_callback", provider="gdrive")
        assert "/user/cloud/gdrive/callback" in uri


class TestGetService:
    @pytest.mark.parametrize("provider,module_attr", [
        ("gdrive", "gdrive_service"),
        ("onedrive", "onedrive_service"),
        ("dropbox", "dropbox_service"),
        ("kdrive", "kdrive_service"),
        ("zotero", "zotero_service"),
        ("s3", "s3_service"),
    ])
    def test_returns_correct_service_module(self, provider, module_attr):
        from app.routes.user_cloud import _get_service
        svc = _get_service(provider)
        assert svc is not None
        assert module_attr in svc.__name__

    def test_unknown_provider_returns_none(self):
        from app.routes.user_cloud import _get_service
        assert _get_service("yahoo-cloud") is None


class TestOAuthStateStorage:
    def test_session_fallback_works_when_redis_unavailable(self, app):
        from app.routes.user_cloud import _pop_oauth_state, _store_oauth_state
        with app.test_request_context():
            with patch("app.routes.user_cloud._redis", side_effect=Exception("no redis")):
                _store_oauth_state("gdrive", "state-abc", user_id=42)
                # Same call retrieves it
                ok = _pop_oauth_state("gdrive", "state-abc", user_id=42)
        assert ok is True

    def test_pop_with_wrong_state_returns_false(self, app):
        from app.routes.user_cloud import _pop_oauth_state, _store_oauth_state
        with app.test_request_context():
            with patch("app.routes.user_cloud._redis", side_effect=Exception("no redis")):
                _store_oauth_state("gdrive", "state-abc", 1)
                ok = _pop_oauth_state("gdrive", "wrong-state", 1)
        assert ok is False


# ─────────────────────────────────────────────────────────────────
# Auth gates — anonymous access
# ─────────────────────────────────────────────────────────────────

class TestAnonymousAccess:
    @pytest.mark.parametrize("path", [
        "/user/cloud",
        "/user/cloud/gdrive/connect",
        "/user/cloud/zotero/connect",
        "/user/cloud/kdrive/connect",
        "/user/cloud/s3/connect",
        "/user/cloud/notion/connect",
        "/user/cloud/webdav/connect",
        "/user/cloud/1/folders",
        "/user/cloud/1/documents",
    ])
    def test_get_endpoints_redirect_to_login(self, client, path):
        r = client.get(path, follow_redirects=False)
        # Either redirects to login (302) or returns 401
        assert r.status_code in (302, 401)
        if r.status_code == 302:
            assert "login" in r.headers.get("Location", "").lower()

    @pytest.mark.parametrize("path", [
        "/user/cloud/1/set-folder",
        "/user/cloud/1/sync",
        "/user/cloud/1/disconnect",
        "/user/cloud/1/set-frequency",
    ])
    def test_post_endpoints_redirect_to_login(self, client, path):
        r = client.post(path, follow_redirects=False)
        assert r.status_code in (302, 401)


# ─────────────────────────────────────────────────────────────────
# Subscriber gate — non-subscriber redirected to dashboard
# ─────────────────────────────────────────────────────────────────

class TestSubscriberGate:
    def test_non_subscriber_redirected_away_from_cloud(self, client, app, db, non_subscriber_user):
        """A non-subscriber (unconfirmed) trying to access /cloud is bounced."""
        _login(client, app, db, non_subscriber_user, "StdUser1!")
        r = client.get("/user/cloud", follow_redirects=False)
        assert r.status_code == 302
        loc = r.headers.get("Location", "")
        # Just assert they don't land on the cloud page itself
        assert "/user/cloud" not in loc or loc.endswith("/login")


# ─────────────────────────────────────────────────────────────────
# cloud_connections — main listing
# ─────────────────────────────────────────────────────────────────

class TestCloudConnectionsList:
    def test_pro_user_sees_listing(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.get("/user/cloud")
        assert r.status_code == 200

    def test_listing_includes_existing_connections(self, client, app, db, pro_user):
        from app.models.cloud_connection import CloudConnection
        with app.app_context():
            c = CloudConnection(
                user_id=pro_user, provider="gdrive", folder_id=None,
                access_token_enc="x", refresh_token_enc="x",
            )
            db.session.add(c)
            db.session.commit()
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.get("/user/cloud")
        assert r.status_code == 200


# ─────────────────────────────────────────────────────────────────
# cloud_connect — provider routing + dispatch
# ─────────────────────────────────────────────────────────────────

class TestCloudConnect:
    def test_unknown_provider_redirected_back(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.get("/user/cloud/unknown-provider/connect", follow_redirects=False)
        assert r.status_code == 302
        assert "/user/cloud" in r.headers.get("Location", "")

    def test_zotero_redirects_to_form(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.get("/user/cloud/zotero/connect", follow_redirects=False)
        # Zotero routes to its own form (200 GET) or to the form URL (302)
        assert r.status_code in (200, 302)

    def test_s3_redirects_to_form(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.get("/user/cloud/s3/connect", follow_redirects=False)
        assert r.status_code in (200, 302)

    def test_gdrive_redirects_to_service_account_form(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.get("/user/cloud/gdrive/connect", follow_redirects=False)
        assert r.status_code in (200, 302)


# ─────────────────────────────────────────────────────────────────
# Cloud connections are unlimited for everyone (pivot) — a Free user can
# connect more than one provider. The old per-tier 1-connection cap has been
# removed, so its test is gone.
# ─────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────
# trigger_sync
# ─────────────────────────────────────────────────────────────────

class TestTriggerSync:
    def test_unknown_connection_returns_404(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.post("/user/cloud/99999/sync")
        assert r.status_code == 404

    def test_other_users_connection_returns_404(self, client, app, db, pro_user):
        from app.models.cloud_connection import CloudConnection
        with app.app_context():
            c = CloudConnection(
                user_id=pro_user + 9999, provider="gdrive",  # different user
                folder_id=None, access_token_enc="x", refresh_token_enc="x",
            )
            db.session.add(c)
            db.session.commit()
            cid = c.id

        _login(client, app, db, pro_user, "ProUser1!")
        r = client.post(f"/user/cloud/{cid}/sync")
        assert r.status_code == 404

        with app.app_context():
            db.session.delete(db.session.get(CloudConnection, cid))
            db.session.commit()

    def test_already_syncing_returns_409(self, client, app, db, pro_user):
        from app.models.cloud_connection import CloudConnection
        with app.app_context():
            c = CloudConnection(
                user_id=pro_user, provider="gdrive", folder_id=None,
                access_token_enc="x", refresh_token_enc="x",
                is_syncing=True,
            )
            db.session.add(c)
            db.session.commit()
            cid = c.id

        _login(client, app, db, pro_user, "ProUser1!")
        r = client.post(f"/user/cloud/{cid}/sync")
        assert r.status_code == 409

    def test_dispatches_sync_in_background(self, client, app, db, pro_user):
        from app.models.cloud_connection import CloudConnection
        with app.app_context():
            c = CloudConnection(
                user_id=pro_user, provider="gdrive", folder_id=None,
                access_token_enc="x", refresh_token_enc="x",
            )
            db.session.add(c)
            db.session.commit()
            cid = c.id

        _login(client, app, db, pro_user, "ProUser1!")
        with patch("threading.Thread") as Thread:
            Thread.return_value = MagicMock()
            r = client.post(f"/user/cloud/{cid}/sync")
        assert r.status_code == 200
        body = r.get_json()
        assert body.get("ok") is True
        Thread.assert_called_once()


# ─────────────────────────────────────────────────────────────────
# disconnect
# ─────────────────────────────────────────────────────────────────

class TestDisconnect:
    def test_unknown_connection_redirects_with_flash(
        self, client, app, db, pro_user,
    ):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.post("/user/cloud/99999/disconnect", follow_redirects=False)
        assert r.status_code == 302
        assert "/user/cloud" in r.headers.get("Location", "")

    def test_deletes_connection_and_calls_remove_index(
        self, client, app, db, pro_user,
    ):
        from app.models.cloud_connection import CloudConnection, CloudDocument
        with app.app_context():
            c = CloudConnection(
                user_id=pro_user, provider="gdrive", folder_id=None,
                access_token_enc="x", refresh_token_enc="x",
            )
            db.session.add(c)
            db.session.flush()
            doc = CloudDocument(
                connection_id=c.id, user_id=pro_user,
                remote_id="r1", name="doc.pdf",
                mime_type="application/pdf", file_size=1, chunk_count=3,
            )
            db.session.add(doc)
            db.session.commit()
            cid = c.id

        _login(client, app, db, pro_user, "ProUser1!")
        with patch("app.services.rag_service.remove_cloud_document_from_index") as rm:
            r = client.post(f"/user/cloud/{cid}/disconnect", follow_redirects=False)

        assert r.status_code == 302
        rm.assert_called_once()
        with app.app_context():
            assert db.session.get(CloudConnection, cid) is None


# ─────────────────────────────────────────────────────────────────
# set_folder
# ─────────────────────────────────────────────────────────────────

class TestSetFolder:
    def test_unknown_connection_404(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.post("/user/cloud/99999/set-folder", data={"folder_id": "abc"})
        assert r.status_code == 404

    def test_updates_folder_id(self, client, app, db, pro_user):
        # Bug N7 — gdrive validate_folder_access est appelé. Mock pour tests.
        from unittest.mock import patch

        from app.models.cloud_connection import CloudConnection
        with app.app_context():
            c = CloudConnection(
                user_id=pro_user, provider="gdrive", folder_id=None,
                access_token_enc="x", refresh_token_enc="x",
            )
            db.session.add(c)
            db.session.commit()
            cid = c.id

        _login(client, app, db, pro_user, "ProUser1!")
        with patch("app.services.gdrive_service.validate_folder_access",
                   return_value={"id": "abc-folder-id"}):
            r = client.post(
                f"/user/cloud/{cid}/set-folder",
                json={"folder_id": "abc-folder-id"},
            )
        assert r.status_code in (200, 302)

        with app.app_context():
            c_after = db.session.get(CloudConnection, cid)
            assert c_after.folder_id == "abc-folder-id"


# ─────────────────────────────────────────────────────────────────
# set_sync_frequency
# ─────────────────────────────────────────────────────────────────

class TestSetSyncFrequency:
    def test_pro_user_can_set_hourly_frequency(
        self, client, app, db, pro_user,
    ):
        """Pivot : Pro peut rapprocher les syncs jusqu'à toutes les heures."""
        from app.models.cloud_connection import CloudConnection
        with app.app_context():
            c = CloudConnection(
                user_id=pro_user, provider="gdrive", folder_id=None,
                access_token_enc="x", refresh_token_enc="x",
            )
            db.session.add(c)
            db.session.commit()
            cid = c.id

        _login(client, app, db, pro_user, "ProUser1!")
        r = client.post(
            f"/user/cloud/{cid}/set-frequency",
            json={"minutes": 60},
        )
        assert r.status_code == 200
        with app.app_context():
            assert db.session.get(CloudConnection, cid).sync_frequency_minutes == 60

    def test_pro_user_cannot_go_below_hourly(
        self, client, app, db, pro_user,
    ):
        """15 min n'est pas dans le set Pro (plancher 60) → 400."""
        from app.models.cloud_connection import CloudConnection
        with app.app_context():
            c = CloudConnection(
                user_id=pro_user, provider="gdrive", folder_id=None,
                access_token_enc="x", refresh_token_enc="x",
            )
            db.session.add(c)
            db.session.commit()
            cid = c.id

        _login(client, app, db, pro_user, "ProUser1!")
        r = client.post(
            f"/user/cloud/{cid}/set-frequency",
            json={"minutes": 15},
        )
        assert r.status_code == 400


# ─────────────────────────────────────────────────────────────────
# list_documents
# ─────────────────────────────────────────────────────────────────

class TestListDocuments:
    def test_unknown_connection_404_or_redirect(
        self, client, app, db, pro_user,
    ):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.get("/user/cloud/99999/documents", follow_redirects=False)
        # Either a JSON 404, an HTML 404, or a redirect with flash
        assert r.status_code in (302, 404)


# ─────────────────────────────────────────────────────────────────
# OAuth callback — error paths (state mismatch, missing code)
# ─────────────────────────────────────────────────────────────────

class TestCloudCallback:
    def test_unknown_provider_redirected(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.get(
            "/user/cloud/unknown/callback?code=abc&state=xyz",
            follow_redirects=False,
        )
        # Should not 500 — handled gracefully
        assert r.status_code in (302, 404)

    def test_state_mismatch_aborts(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        # No state was stored → _pop_oauth_state returns False
        r = client.get(
            "/user/cloud/gdrive/callback?code=abc&state=wrong-state",
            follow_redirects=False,
        )
        assert r.status_code in (302, 400, 403)
