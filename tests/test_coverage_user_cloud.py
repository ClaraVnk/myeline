"""Coverage push — user_cloud routes (callbacks, sync, disconnect, folders).

We don't exercise actual OAuth handshakes — providers are mocked at the
``_get_service`` boundary. The goal is to traverse the route logic
(state validation, plan gating, ownership checks, JSON contract).
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest


def _mk_pro_user(db, app, *, plan="premium_pro"):
    from app.models.user import User
    with app.app_context():
        u = User(
            email=f"u-{uuid.uuid4().hex[:8]}@test.local",
            is_confirmed=True, is_active=True,
            twofa_method="totp", totp_secret="JBSWY3DPEHPK3PXP",
            abonnement_type=plan,
        )
        u.set_password("x")
        db.session.add(u)
        db.session.commit()
        return u.id


def _mk_standard_user(db, app):
    from app.models.user import User
    with app.app_context():
        u = User(
            email=f"std-{uuid.uuid4().hex[:8]}@test.local",
            is_confirmed=True, is_active=True,
            twofa_method="totp", totp_secret="JBSWY3DPEHPK3PXP",
            abonnement_type="premium_pro",
        )
        u.set_password("x")
        db.session.add(u)
        db.session.commit()
        return u.id


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def _mk_conn(db, app, user_id, *, provider="gdrive", **extra):
    from app.models.cloud_connection import CloudConnection
    with app.app_context():
        conn = CloudConnection(
            user_id=user_id, provider=provider,
            access_token_enc="enc", **extra,
        )
        db.session.add(conn)
        db.session.commit()
        return conn.id


# ────────────────────────────────────────────────────────────────────
# /cloud — list
# ────────────────────────────────────────────────────────────────────

class TestCloudList:
    def test_anonymous_redirected(self, client):
        r = client.get("/user/cloud", follow_redirects=False)
        assert r.status_code in (301, 302)

    def test_pro_user_renders_list(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.get("/user/cloud")
        assert r.status_code == 200


# ────────────────────────────────────────────────────────────────────
# /cloud/<provider>/connect — entry point
# ────────────────────────────────────────────────────────────────────

class TestCloudConnect:
    def test_unknown_provider_redirects(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.get("/user/cloud/garbage/connect", follow_redirects=False)
        assert r.status_code in (301, 302)

    def test_zotero_form_renders(self, client, db, app):
        """The /cloud/zotero/connect URL matches the dedicated form route."""
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.get("/user/cloud/zotero/connect")
        assert r.status_code == 200

    def test_kdrive_redirects_to_form(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        # NB: this route is "/cloud/<provider>/connect" but kdrive matches
        # the kdrive_connect_form route directly.
        r = client.get("/user/cloud/kdrive/connect", follow_redirects=False)
        assert r.status_code == 200  # GET on form route

    def test_oauth_provider_redirects_to_idp(self, client, db, app):
        """Generic OAuth connectors call get_auth_url and 302 to the IdP."""
        from app.models.cloud_connection import CloudConnection
        uid = _mk_pro_user(db, app)
        _login(client, uid)

        fake_svc = MagicMock()
        fake_svc.get_auth_url.return_value = ("https://idp/auth?x=1", "STATE-123")
        with patch("app.routes.user_cloud._get_service", return_value=fake_svc):
            r = client.get("/user/cloud/onedrive/connect", follow_redirects=False)
        assert r.status_code in (301, 302)
        loc = r.headers.get("Location") or ""
        assert "idp/auth" in loc

    def test_get_auth_url_failure_returns_to_list(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        fake_svc = MagicMock()
        fake_svc.get_auth_url.side_effect = RuntimeError("boom")
        with patch("app.routes.user_cloud._get_service", return_value=fake_svc):
            r = client.get("/user/cloud/dropbox/connect", follow_redirects=False)
        assert r.status_code in (301, 302)
        assert "/user/cloud" in (r.headers.get("Location") or "")


# ────────────────────────────────────────────────────────────────────
# OAuth callback
# ────────────────────────────────────────────────────────────────────

class TestCloudCallback:
    def test_unknown_provider(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.get("/user/cloud/garbage/callback?code=x&state=y",
                       follow_redirects=False)
        assert r.status_code in (301, 302)

    def test_missing_code_or_state(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.get("/user/cloud/gdrive/callback", follow_redirects=False)
        assert r.status_code in (301, 302)

    def test_state_mismatch(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        with patch("app.routes.user_cloud._pop_oauth_state",
                   return_value=False):
            r = client.get(
                "/user/cloud/gdrive/callback?code=c&state=fake",
                follow_redirects=False,
            )
        assert r.status_code in (301, 302)

    def test_happy_personal_callback_creates_connection(self, client, db, app):
        from app.models.cloud_connection import CloudConnection
        uid = _mk_pro_user(db, app)
        _login(client, uid)

        def _store(conn, code, redirect_uri):
            conn.access_token_enc = "real-enc"

        fake_svc = MagicMock()
        fake_svc.store_tokens.side_effect = _store
        with patch("app.routes.user_cloud._pop_oauth_state", return_value=True), \
             patch("app.routes.user_cloud._get_service", return_value=fake_svc):
            r = client.get(
                "/user/cloud/gdrive/callback?code=CODE&state=STATE",
                follow_redirects=False,
            )
        assert r.status_code in (301, 302)
        with app.app_context():
            assert CloudConnection.query.filter_by(
                user_id=uid, provider="gdrive"
            ).count() == 1

    def test_callback_store_tokens_failure_redirects(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        fake_svc = MagicMock()
        fake_svc.store_tokens.side_effect = RuntimeError("oauth fail")
        with patch("app.routes.user_cloud._pop_oauth_state", return_value=True), \
             patch("app.routes.user_cloud._get_service", return_value=fake_svc):
            r = client.get(
                "/user/cloud/dropbox/callback?code=CODE&state=STATE",
                follow_redirects=False,
            )
        assert r.status_code in (301, 302)


# ────────────────────────────────────────────────────────────────────
# /cloud/<id>/folders + set-folder
# ────────────────────────────────────────────────────────────────────

class TestFolderPicker:
    def test_unknown_connection_redirects(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.get("/user/cloud/9999/folders", follow_redirects=False)
        assert r.status_code in (301, 302)

    def test_renders_folder_list(self, client, db, app):
        uid = _mk_pro_user(db, app)
        cid = _mk_conn(db, app, uid, provider="gdrive")
        _login(client, uid)
        fake_svc = MagicMock()
        fake_svc.list_folders.return_value = [
            {"id": "f1", "name": "Reports"},
        ]
        with patch("app.routes.user_cloud._get_service", return_value=fake_svc):
            r = client.get(f"/user/cloud/{cid}/folders")
        assert r.status_code == 200

    def test_renders_when_list_folders_throws(self, client, db, app):
        uid = _mk_pro_user(db, app)
        cid = _mk_conn(db, app, uid, provider="gdrive")
        _login(client, uid)
        fake_svc = MagicMock()
        fake_svc.list_folders.side_effect = RuntimeError("api down")
        with patch("app.routes.user_cloud._get_service", return_value=fake_svc):
            r = client.get(f"/user/cloud/{cid}/folders")
        assert r.status_code == 200


class TestSetFolder:
    def test_unknown_connection_returns_404(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.post("/user/cloud/9999/set-folder",
                        json={"folder_id": "f1", "folder_name": "Reports"})
        assert r.status_code == 404

    def test_other_user_connection_returns_404(self, client, db, app):
        owner = _mk_pro_user(db, app)
        intruder = _mk_pro_user(db, app)
        cid = _mk_conn(db, app, owner)
        _login(client, intruder)
        r = client.post(f"/user/cloud/{cid}/set-folder",
                        json={"folder_id": "f1"})
        assert r.status_code == 404

    def test_sets_folder_and_resets_cursor(self, client, db, app):
        # Bug N7 (audit round 2) — gdrive validate_folder_access est
        # appelé sur set_folder pour valider l'existence du folder.
        # Mock pour ne pas hit l'API Google en test.
        from unittest.mock import patch

        from app.models.cloud_connection import CloudConnection
        uid = _mk_pro_user(db, app)
        cid = _mk_conn(db, app, uid, sync_cursor="OLD")
        _login(client, uid)
        with patch("app.services.gdrive_service.validate_folder_access",
                   return_value={"id": "F1", "name": "MyDir"}):
            r = client.post(f"/user/cloud/{cid}/set-folder",
                            json={"folder_id": "F1", "folder_name": "MyDir"})
        assert r.status_code == 200
        assert r.get_json() == {"ok": True}
        with app.app_context():
            conn = db.session.get(CloudConnection, cid)
            assert conn.folder_id == "F1"
            assert conn.folder_name == "MyDir"
            assert conn.sync_cursor is None


# ────────────────────────────────────────────────────────────────────
# Trigger sync
# ────────────────────────────────────────────────────────────────────

class TestTriggerSync:
    def test_unknown_connection_returns_404(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.post("/user/cloud/9999/sync")
        assert r.status_code == 404

    def test_already_syncing_returns_409(self, client, db, app):
        uid = _mk_pro_user(db, app)
        cid = _mk_conn(db, app, uid, is_syncing=True)
        _login(client, uid)
        r = client.post(f"/user/cloud/{cid}/sync")
        assert r.status_code == 409

    def test_starts_sync_thread(self, client, db, app):
        uid = _mk_pro_user(db, app)
        cid = _mk_conn(db, app, uid)
        _login(client, uid)
        fake_svc = MagicMock()
        fake_thread = MagicMock()
        with patch("app.routes.user_cloud._get_service", return_value=fake_svc), \
             patch("app.routes.user_cloud.threading.Thread",
                   return_value=fake_thread) as mock_thread:
            r = client.post(f"/user/cloud/{cid}/sync")
        assert r.status_code == 200
        body = r.get_json()
        assert body.get("ok") is True
        mock_thread.assert_called_once()


# ────────────────────────────────────────────────────────────────────
# Sync status
# ────────────────────────────────────────────────────────────────────

class TestSyncStatus:
    def test_returns_empty_list_when_no_active_syncs(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.get("/user/cloud/sync-status")
        assert r.status_code == 200
        assert r.get_json() == {"syncs": []}

    def test_returns_in_flight_sync(self, client, db, app):
        from datetime import datetime, timedelta, timezone
        uid = _mk_pro_user(db, app)
        cid = _mk_conn(
            db, app, uid,
            is_syncing=True,
            sync_total_files=100,
            sync_processed_files=10,
            sync_started_at=datetime.now(timezone.utc) - timedelta(seconds=20),
        )
        _login(client, uid)
        r = client.get("/user/cloud/sync-status")
        assert r.status_code == 200
        body = r.get_json()
        assert len(body["syncs"]) == 1
        s = body["syncs"][0]
        assert s["id"] == cid
        assert s["processed"] == 10
        assert s["total"] == 100


# ────────────────────────────────────────────────────────────────────
# Disconnect
# ────────────────────────────────────────────────────────────────────

class TestDisconnect:
    def test_unknown_connection_redirects(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.post("/user/cloud/9999/disconnect", follow_redirects=False)
        assert r.status_code in (301, 302)

    def test_other_users_connection_redirects(self, client, db, app):
        owner = _mk_pro_user(db, app)
        intruder = _mk_pro_user(db, app)
        cid = _mk_conn(db, app, owner)
        _login(client, intruder)
        r = client.post(f"/user/cloud/{cid}/disconnect", follow_redirects=False)
        assert r.status_code in (301, 302)

    def test_happy_path_deletes_connection(self, client, db, app):
        from app.models.cloud_connection import CloudConnection
        uid = _mk_pro_user(db, app)
        cid = _mk_conn(db, app, uid)
        _login(client, uid)
        r = client.post(f"/user/cloud/{cid}/disconnect", follow_redirects=False)
        assert r.status_code in (301, 302)
        with app.app_context():
            assert db.session.get(CloudConnection, cid) is None


# ────────────────────────────────────────────────────────────────────
# Set sync frequency
# ────────────────────────────────────────────────────────────────────

class TestSetSyncFrequency:
    def test_unknown_connection_returns_404(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.post("/user/cloud/9999/set-frequency", json={"minutes": 60})
        assert r.status_code == 404

    def test_can_set_hourly(self, client, db, app):
        uid = _mk_pro_user(db, app)
        cid = _mk_conn(db, app, uid)
        _login(client, uid)
        r = client.post(f"/user/cloud/{cid}/set-frequency", json={"minutes": 60})
        assert r.status_code == 200

    def test_can_set_valid_frequency(self, client, db, app):
        from app.models.cloud_connection import CloudConnection
        uid = _mk_pro_user(db, app)
        cid = _mk_conn(db, app, uid)
        _login(client, uid)
        r = client.post(f"/user/cloud/{cid}/set-frequency", json={"minutes": 60})
        assert r.status_code == 200
        assert r.get_json() == {"success": True}
        with app.app_context():
            assert db.session.get(CloudConnection, cid).sync_frequency_minutes == 60

    def test_invalid_frequency_rejected(self, client, db, app):
        uid = _mk_pro_user(db, app)
        cid = _mk_conn(db, app, uid)
        _login(client, uid)
        # 5 minutes is not in the allowed set
        r = client.post(f"/user/cloud/{cid}/set-frequency", json={"minutes": 5})
        assert r.status_code == 400

    def test_invalid_minutes_value(self, client, db, app):
        uid = _mk_pro_user(db, app)
        cid = _mk_conn(db, app, uid)
        _login(client, uid)
        r = client.post(f"/user/cloud/{cid}/set-frequency",
                        json={"minutes": "not-a-number"})
        assert r.status_code == 400


# ────────────────────────────────────────────────────────────────────
# Document list
# ────────────────────────────────────────────────────────────────────

class TestListDocuments:
    def test_unknown_connection_returns_404(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.get("/user/cloud/9999/documents")
        assert r.status_code == 404

    def test_other_users_connection_returns_404(self, client, db, app):
        owner = _mk_pro_user(db, app)
        intruder = _mk_pro_user(db, app)
        cid = _mk_conn(db, app, owner)
        _login(client, intruder)
        r = client.get(f"/user/cloud/{cid}/documents")
        assert r.status_code == 404

    def test_returns_doc_list_for_owner(self, client, db, app):
        from app.models.cloud_connection import CloudDocument
        uid = _mk_pro_user(db, app)
        cid = _mk_conn(db, app, uid)
        with app.app_context():
            doc = CloudDocument(
                connection_id=cid, user_id=uid, remote_id="r1",
                name="A.pdf", chunk_count=2,
            )
            db.session.add(doc)
            db.session.commit()
        _login(client, uid)
        r = client.get(f"/user/cloud/{cid}/documents")
        assert r.status_code == 200
        body = r.get_json()
        assert isinstance(body, list)
        assert any(d["name"] == "A.pdf" for d in body)
