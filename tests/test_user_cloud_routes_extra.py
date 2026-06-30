"""Supplementary user_cloud route tests targeting the form handlers
(zotero/kdrive/s3/notion/webdav), folder picker, and OAuth callbacks
that the existing test_user_cloud_routes.py barely touches.
"""
import uuid
from unittest.mock import MagicMock, patch

import pytest

from tests.helpers import login


# ─── Fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def pro_user(app, db):
    from app.models.user import User
    with app.app_context():
        u = User(
            email=f"cl-pro-{uuid.uuid4().hex[:6]}@example.com",
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


def _email(app, db, user_id):
    from app.models.user import User
    with app.app_context():
        u = db.session.get(User, user_id)
        return u.email


# ─── Connect-form GET pages ──────────────────────────────────────────

class TestConnectFormPages:
    def test_zotero_form_renders(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProUser1!")
        r = client.get("/user/cloud/zotero/connect")
        assert r.status_code == 200

    def test_kdrive_form_renders(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProUser1!")
        r = client.get("/user/cloud/kdrive/connect")
        assert r.status_code == 200

    def test_s3_form_renders(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProUser1!")
        r = client.get("/user/cloud/s3/connect")
        assert r.status_code == 200

    def test_notion_form_renders(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProUser1!")
        r = client.get("/user/cloud/notion/connect")
        assert r.status_code == 200

    def test_webdav_form_renders(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProUser1!")
        r = client.get("/user/cloud/webdav/connect")
        assert r.status_code == 200


# ─── Zotero POST validation ──────────────────────────────────────────

class TestZoteroFormPost:
    def test_missing_fields_returns_form(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProUser1!")
        r = client.post("/user/cloud/zotero/connect",
                        data={"api_key": "", "user_id": ""})
        assert r.status_code == 200  # re-renders form

    def test_persists_credentials_on_success(self, client, app, db, pro_user):
        from app.models.cloud_connection import CloudConnection
        login(client, _email(app, db, pro_user), "ProUser1!")
        r = client.post(
            "/user/cloud/zotero/connect",
            data={"api_key": "ZK-secret", "user_id": "98765"},
            follow_redirects=False,
        )
        assert r.status_code in (301, 302)
        with app.app_context():
            conn = CloudConnection.query.filter_by(
                user_id=pro_user, provider="zotero",
            ).first()
        assert conn is not None
        assert conn.folder_id == "98765"


# ─── kDrive POST validation ──────────────────────────────────────────

class TestKdriveFormPost:
    def test_missing_fields(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProUser1!")
        r = client.post("/user/cloud/kdrive/connect",
                        data={"email": "", "kdrive_id": "", "app_password": ""})
        assert r.status_code == 200

    def test_non_numeric_drive_id_rejected(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProUser1!")
        r = client.post("/user/cloud/kdrive/connect",
                        data={"email": "u@example.com",
                              "kdrive_id": "abc",
                              "app_password": "pwd"})
        assert r.status_code == 200

    def test_bad_auth_re_renders(self, client, app, db, pro_user):
        from app.services import kdrive_service
        login(client, _email(app, db, pro_user), "ProUser1!")
        with patch.object(kdrive_service, "check_credentials",
                          return_value=kdrive_service._CRED_BAD_AUTH):
            r = client.post("/user/cloud/kdrive/connect",
                            data={"email": "u@example.com",
                                  "kdrive_id": "12345",
                                  "app_password": "wrong"})
        assert r.status_code == 200

    def test_success_persists_and_redirects(self, client, app, db, pro_user):
        from app.models.cloud_connection import CloudConnection
        from app.services import kdrive_service
        login(client, _email(app, db, pro_user), "ProUser1!")
        with patch.object(kdrive_service, "check_credentials",
                          return_value=kdrive_service._CRED_OK):
            r = client.post(
                "/user/cloud/kdrive/connect",
                data={"email": "u@example.com",
                      "kdrive_id": "12345",
                      "app_password": "AppPwd"},
                follow_redirects=False,
            )
        assert r.status_code in (301, 302)
        with app.app_context():
            conn = CloudConnection.query.filter_by(
                user_id=pro_user, provider="kdrive",
            ).first()
        assert conn is not None


# ─── S3 POST validation ──────────────────────────────────────────────

class TestS3FormPost:
    def test_missing_fields(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProUser1!")
        r = client.post("/user/cloud/s3/connect",
                        data={"access_key": "", "secret_key": "",
                              "bucket_name": ""})
        assert r.status_code == 200

    def test_test_connection_failure_rerenders(self, client, app, db, pro_user):
        from app.services import s3_service
        login(client, _email(app, db, pro_user), "ProUser1!")
        with patch.object(s3_service, "test_connection",
                          side_effect=Exception("boom")):
            r = client.post(
                "/user/cloud/s3/connect",
                data={"access_key": "AK", "secret_key": "SK",
                      "bucket_name": "b"},
            )
        assert r.status_code == 200

    def test_success_persists(self, client, app, db, pro_user):
        from app.models.cloud_connection import CloudConnection
        from app.services import s3_service
        login(client, _email(app, db, pro_user), "ProUser1!")
        with patch.object(s3_service, "test_connection",
                          return_value=True):
            r = client.post(
                "/user/cloud/s3/connect",
                data={"access_key": "AK", "secret_key": "SK",
                      "bucket_name": "b"},
                follow_redirects=False,
            )
        assert r.status_code in (301, 302)
        with app.app_context():
            conn = CloudConnection.query.filter_by(
                user_id=pro_user, provider="s3",
            ).first()
        assert conn is not None


# ─── Notion POST validation ──────────────────────────────────────────

class TestNotionFormPost:
    def test_missing_token(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProUser1!")
        r = client.post("/user/cloud/notion/connect", data={"token": ""})
        assert r.status_code == 200

    def test_invalid_token(self, client, app, db, pro_user):
        from app.services import notion_service
        login(client, _email(app, db, pro_user), "ProUser1!")
        with patch.object(notion_service, "check_credentials",
                          return_value=False):
            r = client.post("/user/cloud/notion/connect",
                            data={"token": "bad-token"})
        assert r.status_code == 200

    def test_success(self, client, app, db, pro_user):
        from app.models.cloud_connection import CloudConnection
        from app.services import notion_service
        login(client, _email(app, db, pro_user), "ProUser1!")
        with patch.object(notion_service, "check_credentials",
                          return_value=True):
            r = client.post("/user/cloud/notion/connect",
                            data={"token": "secret_xyz"},
                            follow_redirects=False)
        assert r.status_code in (301, 302)
        with app.app_context():
            conn = CloudConnection.query.filter_by(
                user_id=pro_user, provider="notion",
            ).first()
        assert conn is not None


# ─── WebDAV POST validation ──────────────────────────────────────────

class TestWebdavFormPost:
    def test_missing_fields(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProUser1!")
        r = client.post("/user/cloud/webdav/connect",
                        data={"url": "", "username": "", "password": ""})
        assert r.status_code == 200

    def test_bad_auth_status(self, client, app, db, pro_user):
        from app.services import webdav_service
        login(client, _email(app, db, pro_user), "ProUser1!")
        with patch.object(webdav_service, "check_credentials",
                          return_value="bad_auth"):
            r = client.post(
                "/user/cloud/webdav/connect",
                data={"url": "https://nx.example.com",
                      "username": "u", "password": "p"},
            )
        assert r.status_code == 200

    def test_success(self, client, app, db, pro_user):
        from app.models.cloud_connection import CloudConnection
        from app.services import webdav_service
        login(client, _email(app, db, pro_user), "ProUser1!")
        with patch.object(webdav_service, "check_credentials",
                          return_value="ok"):
            r = client.post(
                "/user/cloud/webdav/connect",
                data={"url": "https://nx.example.com",
                      "username": "u", "password": "p"},
                follow_redirects=False,
            )
        assert r.status_code in (301, 302)
        with app.app_context():
            conn = CloudConnection.query.filter_by(
                user_id=pro_user, provider="webdav",
            ).first()
        assert conn is not None


# ─── OAuth callback edges ─────────────────────────────────────────────

class TestOAuthCallback:
    def test_unknown_provider_redirects(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProUser1!")
        r = client.get("/user/cloud/bogus/callback?code=x&state=y",
                       follow_redirects=False)
        assert r.status_code in (301, 302)

    def test_missing_code_redirects_with_flash(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProUser1!")
        r = client.get("/user/cloud/gdrive/callback?state=x",
                       follow_redirects=False)
        assert r.status_code in (301, 302)

    def test_state_mismatch_redirects(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProUser1!")
        r = client.get("/user/cloud/gdrive/callback?code=c&state=unknown",
                       follow_redirects=False)
        assert r.status_code in (301, 302)


# ─── Folder picker / set_folder ───────────────────────────────────────

class TestFolderPicker:
    def test_lists_folders_for_owner(self, client, app, db, pro_user):
        from app.models.cloud_connection import CloudConnection
        from app.services import onedrive_service
        login(client, _email(app, db, pro_user), "ProUser1!")
        with app.app_context():
            c = CloudConnection(
                user_id=pro_user, provider="onedrive",
                access_token_enc="x", refresh_token_enc="x",
            )
            db.session.add(c)
            db.session.commit()
            cid = c.id

        with patch.object(onedrive_service, "list_folders",
                          return_value=[{"id": "F1", "name": "Docs"}]):
            r = client.get(f"/user/cloud/{cid}/folders")
        assert r.status_code == 200
        assert b"Docs" in r.data

    def test_other_users_connection_404(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProUser1!")
        r = client.get("/user/cloud/9999999/folders",
                       follow_redirects=False)
        # Either 404 or redirect depending on guard
        assert r.status_code in (302, 404)
