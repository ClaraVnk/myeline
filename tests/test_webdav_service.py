"""Coverage for app/services/webdav_service.py — generic WebDAV connector.

The HTTP boundary (`requests.request`, `requests.get`) is mocked.
"""
from unittest.mock import MagicMock, patch


# ─── credentials round-trip ──────────────────────────────────────────

class TestCredentials:
    def test_store_and_retrieve(self, app):
        from app.services.webdav_service import _get_credentials, store_credentials
        with app.test_request_context():
            conn = MagicMock(spec=[
                "access_token_enc", "refresh_token_enc",
                "token_expiry", "sync_cursor",
            ])
            store_credentials(conn, "https://nx.example.com/", "u", "p")
            base, user, pwd = _get_credentials(conn)
        assert base == "https://nx.example.com"
        assert user == "u"
        assert pwd == "p"


# ─── check_credentials ───────────────────────────────────────────────

class TestCheckCredentials:
    @staticmethod
    def _resp(status):
        r = MagicMock(status_code=status)
        return r

    def test_ok_on_207(self):
        from app.services.webdav_service import check_credentials
        with patch("app.services.webdav_service.safe_request",
                   return_value=self._resp(207)):
            assert check_credentials("https://nx.example.com", "u", "p") == "ok"

    def test_ok_on_200(self):
        from app.services.webdav_service import check_credentials
        with patch("app.services.webdav_service.safe_request",
                   return_value=self._resp(200)):
            assert check_credentials("https://nx.example.com", "u", "p") == "ok"

    def test_bad_auth_on_401(self):
        from app.services.webdav_service import check_credentials
        with patch("app.services.webdav_service.safe_request",
                   return_value=self._resp(401)):
            assert check_credentials("https://nx.example.com", "u", "p") == "bad_auth"

    def test_error_on_500(self):
        from app.services.webdav_service import check_credentials
        with patch("app.services.webdav_service.safe_request",
                   return_value=self._resp(500)):
            assert check_credentials("https://nx.example.com", "u", "p") == "error"

    def test_error_on_exception(self):
        from app.services.webdav_service import check_credentials
        with patch("app.services.webdav_service.safe_request",
                   side_effect=Exception("net")):
            assert check_credentials("https://nx.example.com", "u", "p") == "error"


# ─── _propfind XML parsing ───────────────────────────────────────────

_PROPFIND_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:">
  <d:response>
    <d:href>/Documents/</d:href>
    <d:propstat><d:prop>
      <d:displayname>Documents</d:displayname>
      <d:resourcetype><d:collection/></d:resourcetype>
      <d:getcontentlength>0</d:getcontentlength>
      <d:getlastmodified>Mon, 04 Jan 2026 10:00:00 GMT</d:getlastmodified>
      <d:getcontenttype></d:getcontenttype>
    </d:prop></d:propstat>
  </d:response>
  <d:response>
    <d:href>/Documents/notes.pdf</d:href>
    <d:propstat><d:prop>
      <d:displayname>notes.pdf</d:displayname>
      <d:resourcetype/>
      <d:getcontentlength>4096</d:getcontentlength>
      <d:getlastmodified>Tue, 05 Jan 2026 11:30:00 GMT</d:getlastmodified>
      <d:getcontenttype>application/pdf</d:getcontenttype>
    </d:prop></d:propstat>
  </d:response>
</d:multistatus>"""


class TestPropfind:
    def test_parses_collection_and_file(self):
        from app.services.webdav_service import _propfind
        resp = MagicMock(content=_PROPFIND_BODY)
        resp.raise_for_status = MagicMock()
        with patch("app.services.webdav_service.safe_request",
                   return_value=resp):
            results = _propfind(("u", "p"), "https://nx.example.com", "/")
        assert len(results) == 2
        folder = next(r for r in results if r["is_dir"])
        assert folder["name"] == "Documents"
        f = next(r for r in results if not r["is_dir"])
        assert f["size"] == 4096
        assert f["content_type"] == "application/pdf"
        assert f["last_modified"] is not None


# ─── list_folders ────────────────────────────────────────────────────

class TestListFolders:
    def _make_conn(self, app):
        from app.services.webdav_service import store_credentials
        conn = MagicMock(spec=[
            "access_token_enc", "refresh_token_enc",
            "token_expiry", "sync_cursor", "folder_id",
        ])
        conn.folder_id = "/"
        with app.test_request_context():
            store_credentials(conn, "https://nx.example.com/", "u", "p")
        return conn

    def test_returns_only_directories(self, app):
        from app.services import webdav_service
        with app.test_request_context():
            conn = self._make_conn(app)
            with patch.object(webdav_service, "_propfind") as mock_pf:
                mock_pf.return_value = [
                    {"href": "/Docs", "name": "Docs", "is_dir": True,
                     "size": 0, "last_modified": None, "content_type": ""},
                    {"href": "/file.pdf", "name": "file.pdf",
                     "is_dir": False, "size": 1, "last_modified": None,
                     "content_type": "application/pdf"},
                ]
                folders = webdav_service.list_folders(conn)
        assert {f["name"] for f in folders} == {"Docs"}

    def test_returns_empty_on_propfind_error(self, app):
        from app.services import webdav_service
        with app.test_request_context():
            conn = self._make_conn(app)
            with patch.object(webdav_service, "_propfind",
                              side_effect=Exception("network")):
                assert webdav_service.list_folders(conn) == []


# ─── _list_files_recursive ───────────────────────────────────────────

class TestListFilesRecursive:
    def test_bfs_with_indexable_filter(self):
        from app.services import webdav_service
        def fake_pf(auth, base_url, path, depth=1):
            if path == "/":
                return [
                    {"href": "/Docs", "name": "Docs", "is_dir": True,
                     "size": 0, "last_modified": None, "content_type": ""},
                ]
            if path.rstrip("/") == "/Docs":
                return [
                    {"href": "/Docs/n.pdf", "name": "n.pdf",
                     "is_dir": False, "size": 100,
                     "last_modified": None,
                     "content_type": "application/pdf"},
                    {"href": "/Docs/img.heic", "name": "img.heic",
                     "is_dir": False, "size": 100,
                     "last_modified": None,
                     "content_type": "image/heic"},
                ]
            return []

        with patch.object(webdav_service, "_propfind", side_effect=fake_pf):
            results = webdav_service._list_files_recursive(
                ("u", "p"), "https://nx.example.com", "/",
            )
        names = {r["name"] for r in results}
        assert "n.pdf" in names
        assert "img.heic" not in names

    def test_continues_on_propfind_failure(self):
        from app.services import webdav_service
        with patch.object(webdav_service, "_propfind",
                          side_effect=Exception("transient")):
            assert webdav_service._list_files_recursive(
                ("u", "p"), "https://nx.example.com", "/",
            ) == []


# ─── sync_connection ─────────────────────────────────────────────────

class TestSyncConnection:
    def test_returns_error_when_connection_missing(self, app):
        from app.services.webdav_service import sync_connection
        with app.app_context():
            with patch("app.ext.db.session.get", return_value=None):
                result = sync_connection(999_999)
        assert "error" in result

    def test_processes_indexable_files(self, app, db):
        import uuid

        from app.models.cloud_connection import CloudConnection
        from app.models.user import User
        from app.services import webdav_service
        with app.app_context():
            u = User(
                email=f"wd-{uuid.uuid4().hex[:6]}@example.com",
                is_confirmed=True, is_active=True,
                abonnement_type="premium_pro",
            )
            u.set_password("Wd1234!")
            db.session.add(u)
            db.session.flush()
            c = CloudConnection(
                user_id=u.id, provider="webdav",
                access_token_enc="x", refresh_token_enc=None,
                folder_id="/",
            )
            db.session.add(c)
            db.session.commit()
            cid = c.id
            webdav_service.store_credentials(c, "https://nx.example.com", "u", "p")
            db.session.commit()

            with patch.object(webdav_service, "_list_files_recursive",
                              return_value=[
                                  {"href": "/Docs/n.pdf", "name": "n.pdf",
                                   "size": 100, "_mime": "application/pdf",
                                   "content_type": "application/pdf",
                                   "last_modified": None},
                              ]), \
                 patch.object(webdav_service, "process_and_index") as mock_pi:
                webdav_service.sync_connection(cid)
        assert mock_pi.called
