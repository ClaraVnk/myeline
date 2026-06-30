"""Coverage for app/services/kdrive_service.py — Infomaniak kDrive WebDAV.

The HTTP boundary (`requests.request`, `requests.get`) is mocked; the rest of
the code path runs end-to-end (XML parsing, BFS traversal, descriptor build).
"""
import json
from unittest.mock import MagicMock, patch


# ─── URL helpers ──────────────────────────────────────────────────────

class TestUrlHelpers:
    def test_base_url_includes_drive_id(self):
        from app.services.kdrive_service import _base_url
        assert _base_url("12345") == "https://12345.connect.kdrive.infomaniak.com"

    def test_dav_url_root(self):
        from app.services.kdrive_service import _dav_url
        assert _dav_url("12345", "/") == "https://12345.connect.kdrive.infomaniak.com/"
        assert _dav_url("12345", "") == "https://12345.connect.kdrive.infomaniak.com/"

    def test_dav_url_subpath_normalises_slashes(self):
        from app.services.kdrive_service import _dav_url
        assert _dav_url("12345", "/foo/bar") == "https://12345.connect.kdrive.infomaniak.com/foo/bar"
        assert _dav_url("12345", "foo/bar") == "https://12345.connect.kdrive.infomaniak.com/foo/bar"


# ─── credentials round-trip ───────────────────────────────────────────

class TestStoreCredentials:
    def test_encrypts_and_clears_token_expiry(self, app):
        from app.services.kdrive_service import store_credentials
        with app.test_request_context():
            conn = MagicMock(spec=[
                "access_token_enc", "refresh_token_enc",
                "token_expiry", "sync_cursor",
            ])
            store_credentials(conn, "u@example.com", "AppPwd123", "98765")
        assert "u@example.com" not in (conn.access_token_enc or "")
        assert "AppPwd123" not in (conn.refresh_token_enc or "")
        assert conn.token_expiry is None
        assert conn.sync_cursor is None


class TestGetCredentials:
    def test_round_trips_credentials(self, app):
        from app.services.kdrive_service import _get_credentials, store_credentials
        with app.test_request_context():
            conn = MagicMock(spec=[
                "access_token_enc", "refresh_token_enc",
                "token_expiry", "sync_cursor",
            ])
            store_credentials(conn, "u@example.com", "AppPwd123", "98765")
            email, pwd, drive_id = _get_credentials(conn)
        assert email == "u@example.com"
        assert pwd == "AppPwd123"
        assert drive_id == "98765"


# ─── check_credentials ────────────────────────────────────────────────

class TestCheckCredentials:
    @staticmethod
    def _fake_resp(status: int):
        r = MagicMock()
        r.status_code = status
        return r

    def test_ok_when_207(self):
        from app.services.kdrive_service import _CRED_OK, check_credentials
        with patch("app.services.kdrive_service.requests.request",
                   return_value=self._fake_resp(207)):
            assert check_credentials("u", "p", "1") == _CRED_OK

    def test_bad_auth_on_401(self):
        from app.services.kdrive_service import _CRED_BAD_AUTH, check_credentials
        with patch("app.services.kdrive_service.requests.request",
                   return_value=self._fake_resp(401)):
            assert check_credentials("u", "p", "1") == _CRED_BAD_AUTH

    def test_no_access_on_403(self):
        from app.services.kdrive_service import _CRED_NO_ACCESS, check_credentials
        with patch("app.services.kdrive_service.requests.request",
                   return_value=self._fake_resp(403)):
            assert check_credentials("u", "p", "1") == _CRED_NO_ACCESS

    def test_not_found_on_404(self):
        from app.services.kdrive_service import _CRED_NOT_FOUND, check_credentials
        with patch("app.services.kdrive_service.requests.request",
                   return_value=self._fake_resp(404)):
            assert check_credentials("u", "p", "1") == _CRED_NOT_FOUND

    def test_error_on_exception(self):
        from app.services.kdrive_service import _CRED_ERROR, check_credentials
        with patch("app.services.kdrive_service.requests.request",
                   side_effect=Exception("network down")):
            assert check_credentials("u", "p", "1") == _CRED_ERROR


# ─── _propfind XML parsing ────────────────────────────────────────────

_PROPFIND_RESPONSE = b"""<?xml version="1.0" encoding="utf-8"?>
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


def _resp_207():
    r = MagicMock()
    r.status_code = 207
    r.content = _PROPFIND_RESPONSE
    r.raise_for_status = MagicMock()
    return r


class TestPropfind:
    def test_parses_collection_and_file(self):
        from app.services.kdrive_service import _propfind
        with patch("app.services.kdrive_service.requests.request",
                   return_value=_resp_207()):
            results = _propfind(("u", "p"), "1", "/")
        assert len(results) == 2
        # Folder
        folder = next(r for r in results if r["is_dir"])
        assert folder["name"] == "Documents"
        # File
        f = next(r for r in results if not r["is_dir"])
        assert f["name"] == "notes.pdf"
        assert f["size"] == 4096
        assert f["content_type"] == "application/pdf"
        assert f["last_modified"] is not None


# ─── list_folders ─────────────────────────────────────────────────────

class TestListFolders:
    def _make_conn(self):
        from app.services.kdrive_service import store_credentials
        conn = MagicMock(spec=[
            "access_token_enc", "refresh_token_enc",
            "token_expiry", "sync_cursor", "folder_id",
        ])
        conn.folder_id = "/"
        store_credentials(conn, "u", "p", "1")
        return conn

    def test_returns_only_directories(self, app):
        from app.services import kdrive_service
        with app.test_request_context():
            conn = self._make_conn()
            with patch.object(kdrive_service, "_propfind") as mock_pf:
                mock_pf.return_value = [
                    {"href": "/Documents", "name": "Documents", "is_dir": True,
                     "size": 0, "last_modified": None, "content_type": ""},
                    {"href": "/file.pdf", "name": "file.pdf", "is_dir": False,
                     "size": 1, "last_modified": None, "content_type": "application/pdf"},
                ]
                folders = kdrive_service.list_folders(conn)
        names = {f["name"] for f in folders}
        assert names == {"Documents"}

    def test_returns_empty_on_propfind_failure(self, app):
        from app.services import kdrive_service
        with app.test_request_context():
            conn = self._make_conn()
            with patch.object(kdrive_service, "_propfind",
                              side_effect=Exception("network")):
                assert kdrive_service.list_folders(conn) == []


# ─── _list_files_recursive ────────────────────────────────────────────

class TestListFilesRecursive:
    def test_bfs_skips_non_indexable_and_recurses(self):
        from app.services import kdrive_service

        # Two-level tree: /Docs → contains /Docs/notes.pdf (indexable) and
        # /Docs/photo.heic (non-indexable). PROPFIND on /Docs returns both.
        def fake_propfind(auth, kdrive_id, path, depth=1):
            if path == "/":
                return [
                    {"href": "/Docs", "name": "Docs", "is_dir": True,
                     "size": 0, "last_modified": None, "content_type": ""},
                ]
            if path == "/Docs":
                return [
                    {"href": "/Docs/notes.pdf", "name": "notes.pdf",
                     "is_dir": False, "size": 100,
                     "last_modified": None, "content_type": "application/pdf"},
                    {"href": "/Docs/photo.heic", "name": "photo.heic",
                     "is_dir": False, "size": 100,
                     "last_modified": None, "content_type": "image/heic"},
                ]
            return []

        with patch.object(kdrive_service, "_propfind", side_effect=fake_propfind):
            results = kdrive_service._list_files_recursive(("u", "p"), "1", "/")
        names = {r["name"] for r in results}
        assert "notes.pdf" in names
        assert "photo.heic" not in names

    def test_continues_on_propfind_error(self):
        from app.services import kdrive_service
        with patch.object(kdrive_service, "_propfind",
                          side_effect=Exception("transient")):
            results = kdrive_service._list_files_recursive(("u", "p"), "1", "/")
        assert results == []


# ─── sync_connection wiring ───────────────────────────────────────────

class TestSyncConnection:
    def test_returns_error_when_connection_missing(self, app):
        from app.services.kdrive_service import sync_connection
        with app.app_context():
            with patch("app.ext.db.session.get", return_value=None):
                result = sync_connection(999_999)
        assert "error" in result

    def test_dispatches_to_full_sync_when_no_cursor(self, app, db):
        from app.services import kdrive_service
        from app.services.kdrive_service import sync_connection
        with app.app_context():
            conn = MagicMock(spec=[
                "id", "is_syncing", "sync_cursor", "folder_id",
                "last_sync_at", "access_token_enc", "refresh_token_enc",
            ])
            conn.id = 1
            conn.is_syncing = False
            conn.sync_cursor = None
            with patch("app.ext.db.session.get", return_value=conn), \
                 patch("app.ext.db.session.commit"), \
                 patch.object(kdrive_service, "_full_sync") as mock_full, \
                 patch.object(kdrive_service, "_delta_sync") as mock_delta:
                sync_connection(1)
        mock_full.assert_called_once()
        mock_delta.assert_not_called()


# ─── _build_index_descriptor ──────────────────────────────────────────

class TestBuildIndexDescriptor:
    def test_returns_none_for_non_indexable(self):
        from app.services import kdrive_service
        item = {"href": "/img.heic", "name": "img.heic", "size": 1,
                "_mime": "image/heic", "last_modified": None,
                "content_type": "image/heic"}
        assert kdrive_service._build_index_descriptor(
            MagicMock(), ("u", "p"), "1", item, {}, existing=None,
        ) is None

    def test_returns_descriptor_for_pdf(self):
        from app.services import kdrive_service
        item = {"href": "/notes.pdf", "name": "notes.pdf", "size": 100,
                "_mime": "application/pdf", "last_modified": None,
                "content_type": "application/pdf"}
        d = kdrive_service._build_index_descriptor(
            MagicMock(), ("u", "p"), "1", item, {"errors": 0}, existing=None,
        )
        assert d is not None
        assert d["name"] == "notes.pdf"
        assert d["mime"] == "application/pdf"
        assert callable(d["download_fn"])
        assert "kdrive.infomaniak.com" in d["web_url"]


# ─── _full_sync / _delta_sync ─────────────────────────────────────────

class TestFullSync:
    def _make_conn(self, app):
        from app.services.kdrive_service import store_credentials
        conn = MagicMock(spec=[
            "id", "is_syncing", "sync_cursor", "folder_id", "last_sync_at",
            "access_token_enc", "refresh_token_enc", "token_expiry",
        ])
        conn.id = 1
        conn.folder_id = "/"
        conn.sync_cursor = None
        with app.test_request_context():
            store_credentials(conn, "u@example.com", "pwd", "1")
        return conn

    def test_walks_tree_and_dispatches_to_indexer(self, app):
        from app.services import kdrive_service
        conn = self._make_conn(app)
        with app.app_context():
            with patch.object(kdrive_service, "_list_files_recursive",
                              return_value=[
                                  {"href": "/a.pdf", "name": "a.pdf",
                                   "size": 10, "_mime": "application/pdf",
                                   "content_type": "application/pdf",
                                   "last_modified": None},
                              ]), \
                 patch("app.services.cloud_sync_utils.parallel_download_then_index") as mock_idx, \
                 patch("app.ext.db.session.commit"):
                kdrive_service._full_sync(conn, {"errors": 0})
        assert mock_idx.called
        batch = mock_idx.call_args.args[0]
        assert len(batch) == 1
        assert batch[0]["name"] == "a.pdf"


class TestDeltaSync:
    def _make_conn(self, app):
        from app.services.kdrive_service import store_credentials
        conn = MagicMock(spec=[
            "id", "is_syncing", "sync_cursor", "folder_id", "last_sync_at",
            "access_token_enc", "refresh_token_enc", "token_expiry",
        ])
        conn.id = 1
        conn.folder_id = "/"
        conn.sync_cursor = "2026-01-01T00:00:00+00:00"
        with app.test_request_context():
            store_credentials(conn, "u@example.com", "pwd", "1")
        return conn

    def test_indexes_new_file_when_no_existing(self, app):
        from datetime import datetime as _dt

        from app.services import kdrive_service
        conn = self._make_conn(app)
        with app.app_context():
            with patch.object(kdrive_service, "_list_files_recursive",
                              return_value=[
                                  {"href": "/a.pdf", "name": "a.pdf",
                                   "size": 10, "_mime": "application/pdf",
                                   "content_type": "application/pdf",
                                   "last_modified": _dt(2025, 12, 1)},
                              ]), \
                 patch("app.services.cloud_sync_utils.parallel_download_then_index") as mock_idx, \
                 patch("app.ext.db.session.commit"):
                stats = {"skipped": 0, "errors": 0}
                kdrive_service._delta_sync(conn, stats)
        # No existing row → file should be queued for indexing.
        assert mock_idx.called
        batch = mock_idx.call_args.args[0]
        assert len(batch) == 1
        assert batch[0]["name"] == "a.pdf"

    def test_falls_back_to_full_sync_path_on_invalid_cursor(self, app):
        """Bad cursor → last_sync=None → no skip path → all files queued."""
        from datetime import datetime as _dt

        from app.services import kdrive_service
        conn = self._make_conn(app)
        conn.sync_cursor = "not-a-date"
        with app.app_context():
            with patch.object(kdrive_service, "_list_files_recursive",
                              return_value=[
                                  {"href": "/a.pdf", "name": "a.pdf",
                                   "size": 10, "_mime": "application/pdf",
                                   "content_type": "application/pdf",
                                   "last_modified": _dt(2025, 12, 1)},
                              ]), \
                 patch("app.services.cloud_sync_utils.parallel_download_then_index") as mock_idx, \
                 patch("app.ext.db.session.commit"):
                stats = {"skipped": 0, "errors": 0}
                kdrive_service._delta_sync(conn, stats)
        assert mock_idx.called
