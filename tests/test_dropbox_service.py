"""Coverage for app/services/dropbox_service.py — Dropbox API v2 connector.

OAuth + API HTTP boundary mocked at `requests.post` / `requests.get`.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch


# ─── pure helpers ────────────────────────────────────────────────────

class TestMimeFromPath:
    def test_pdf(self):
        from app.services.dropbox_service import mime_from_path
        assert mime_from_path("paper.pdf") == "application/pdf"

    def test_unknown_extension_returns_none(self):
        from app.services.dropbox_service import mime_from_path
        assert mime_from_path("blob.heic") is None

    def test_no_extension_returns_none(self):
        from app.services.dropbox_service import mime_from_path
        assert mime_from_path("README") is None


# ─── OAuth ────────────────────────────────────────────────────────────

class TestGetAuthUrl:
    def test_includes_client_id_and_offline_access(self, app):
        from app.services.dropbox_service import get_auth_url
        with app.app_context():
            app.config["DROPBOX_APP_KEY"] = "appk"
            app.config["DROPBOX_APP_SECRET"] = "apps"
            url, state = get_auth_url("https://example.com/cb")
        assert "client_id=appk" in url
        assert "token_access_type=offline" in url
        assert state and len(state) > 16


class TestStoreTokens:
    def test_persists_tokens_and_expiry(self, app):
        from app.services.dropbox_service import store_tokens
        with app.app_context():
            app.config["DROPBOX_APP_KEY"] = "k"
            app.config["DROPBOX_APP_SECRET"] = "s"
            conn = MagicMock(spec=[
                "access_token_enc", "refresh_token_enc", "token_expiry",
            ])
            fake_resp = MagicMock()
            fake_resp.json.return_value = {
                "access_token": "AT",
                "refresh_token": "RT",
                "expires_in": 3600,
            }
            fake_resp.raise_for_status = MagicMock()
            with patch("app.services.dropbox_service.requests.post",
                       return_value=fake_resp):
                store_tokens(conn, "code123", "https://ex/cb")
        # Encrypted-at-rest invariant: the ciphertext must round-trip through
        # decrypt() back to the plaintext, NOT contain the plaintext as a
        # substring. The earlier `"AT" not in enc` / `"RT" not in enc` checks
        # were flaky — Fernet ciphertexts are base64url, so a 2-letter
        # plaintext has a ~1/676 chance of appearing inside any random
        # ciphertext. Round-trip is the proper invariant anyway.
        from app.utils.crypto import decrypt_token
        with app.app_context():
            assert conn.access_token_enc
            assert conn.refresh_token_enc
            assert decrypt_token(conn.access_token_enc) == "AT"
            assert decrypt_token(conn.refresh_token_enc) == "RT"
            assert conn.token_expiry is not None


# ─── _get_token ───────────────────────────────────────────────────────

class TestGetToken:
    def test_returns_cached_token_when_not_expired(self, app):
        from app.services.dropbox_service import _get_token
        from app.utils.crypto import encrypt_token
        with app.app_context():
            conn = MagicMock(spec=[
                "access_token_enc", "refresh_token_enc", "token_expiry",
            ])
            conn.access_token_enc = encrypt_token("AT-cached")
            # Expiry far in the future → no refresh
            conn.token_expiry = (
                datetime.now(timezone.utc).replace(tzinfo=None)
                + timedelta(hours=1)
            )
            with patch("app.services.dropbox_service.requests.post") as mock_post:
                tok = _get_token(conn)
        assert tok == "AT-cached"
        mock_post.assert_not_called()

    def test_refreshes_when_expired(self, app):
        from app.services.dropbox_service import _get_token
        from app.utils.crypto import encrypt_token
        with app.app_context():
            app.config["DROPBOX_APP_KEY"] = "k"
            app.config["DROPBOX_APP_SECRET"] = "s"
            conn = MagicMock(spec=[
                "id", "access_token_enc", "refresh_token_enc", "token_expiry",
            ])
            conn.id = 77
            conn.access_token_enc = encrypt_token("OLD")
            conn.refresh_token_enc = encrypt_token("RT")
            # Expired in the past
            conn.token_expiry = (
                datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(minutes=5)
            )
            fake_resp = MagicMock()
            fake_resp.json.return_value = {"access_token": "NEW", "expires_in": 3600}
            fake_resp.raise_for_status = MagicMock()
            with patch("app.services.dropbox_service.requests.post",
                       return_value=fake_resp), \
                 patch("app.ext.db.session.commit"), \
                 patch("app.ext.db.session.refresh"):
                tok = _get_token(conn)
        assert tok == "NEW"

    def test_returns_static_token_when_no_refresh_token(self, app):
        from app.services.dropbox_service import _get_token
        from app.utils.crypto import encrypt_token
        with app.app_context():
            conn = MagicMock(spec=[
                "access_token_enc", "refresh_token_enc", "token_expiry",
            ])
            conn.access_token_enc = encrypt_token("STATIC")
            conn.refresh_token_enc = None
            conn.token_expiry = None
            tok = _get_token(conn)
        assert tok == "STATIC"


# ─── list_folders ─────────────────────────────────────────────────────

class TestListFolders:
    def test_returns_only_folders(self, app):
        from app.services.dropbox_service import list_folders
        with app.app_context():
            conn = MagicMock(folder_id="")
            fake_resp = MagicMock()
            fake_resp.json.return_value = {
                "entries": [
                    {".tag": "folder", "name": "Docs", "path_lower": "/docs"},
                    {".tag": "file", "name": "a.pdf", "path_lower": "/a.pdf"},
                ]
            }
            fake_resp.raise_for_status = MagicMock()
            with patch("app.services.dropbox_service._auth_header",
                       return_value={"Authorization": "Bearer X"}), \
                 patch("app.services.dropbox_service.requests.post",
                       return_value=fake_resp):
                folders = list_folders(conn)
        assert len(folders) == 1
        assert folders[0]["name"] == "Docs"


# ─── _build_index_descriptor ──────────────────────────────────────────

class TestBuildIndexDescriptor:
    def test_returns_none_for_unsupported_mime(self, app):
        from app.services.dropbox_service import _build_index_descriptor
        with app.app_context():
            conn = MagicMock()
            entry = {"path_lower": "/img.heic", "name": "img.heic", "size": 100}
            assert _build_index_descriptor(conn, entry, {}) is None

    def test_returns_descriptor_for_pdf(self, app):
        from app.services import dropbox_service
        from app.services.dropbox_service import _build_index_descriptor
        with app.app_context():
            conn = MagicMock()
            entry = {"path_lower": "/notes.pdf", "name": "notes.pdf",
                     "size": 100, "server_modified": "2026-01-01T00:00:00Z",
                     "path_display": "/Notes.pdf"}
            with patch.object(dropbox_service, "_get_token", return_value="TOK"):
                d = _build_index_descriptor(conn, entry, {"errors": 0})
        assert d["name"] == "notes.pdf"
        assert d["mime"] == "application/pdf"
        assert callable(d["download_fn"])
        assert "dropbox.com" in d["web_url"]


# ─── sync_connection wiring ───────────────────────────────────────────

class TestSyncConnection:
    def test_returns_error_when_connection_missing(self, app):
        from app.services.dropbox_service import sync_connection
        with app.app_context():
            with patch("app.ext.db.session.get", return_value=None):
                result = sync_connection(999_999)
        assert "error" in result

    def test_dispatches_to_full_when_no_cursor(self, app, db):
        from app.services import dropbox_service
        from app.services.dropbox_service import sync_connection
        with app.app_context():
            conn = MagicMock(spec=[
                "id", "is_syncing", "sync_cursor", "folder_id",
                "last_sync_at",
            ])
            conn.id = 1
            conn.is_syncing = False
            conn.sync_cursor = None
            with patch("app.ext.db.session.get", return_value=conn), \
                 patch("app.ext.db.session.commit"), \
                 patch.object(dropbox_service, "_full_sync") as mock_full, \
                 patch.object(dropbox_service, "_delta_sync") as mock_delta:
                sync_connection(1)
        mock_full.assert_called_once()
        mock_delta.assert_not_called()

    def test_dispatches_to_delta_when_cursor_present(self, app, db):
        from app.services import dropbox_service
        from app.services.dropbox_service import sync_connection
        with app.app_context():
            conn = MagicMock(spec=[
                "id", "is_syncing", "sync_cursor", "folder_id",
                "last_sync_at",
            ])
            conn.id = 1
            conn.is_syncing = False
            conn.sync_cursor = "cursor-abc"
            with patch("app.ext.db.session.get", return_value=conn), \
                 patch("app.ext.db.session.commit"), \
                 patch("app.services.cloud_sync_utils.has_failed_docs",
                       return_value=False), \
                 patch.object(dropbox_service, "_full_sync") as mock_full, \
                 patch.object(dropbox_service, "_delta_sync") as mock_delta:
                sync_connection(1)
        mock_delta.assert_called_once()
        mock_full.assert_not_called()


# ─── _full_sync ───────────────────────────────────────────────────────

class TestFullSync:
    def test_walks_paginated_listing(self, app):
        from app.services import dropbox_service
        with app.app_context():
            conn = MagicMock(spec=[
                "id", "folder_id", "sync_cursor",
                "access_token_enc", "refresh_token_enc", "token_expiry",
            ])
            conn.folder_id = ""

            cursor_resp = MagicMock()
            cursor_resp.json.return_value = {"cursor": "C0"}
            cursor_resp.raise_for_status = MagicMock()

            page1 = MagicMock()
            page1.json.return_value = {
                "entries": [{".tag": "file", "name": "a.pdf",
                             "path_lower": "/a.pdf",
                             "path_display": "/a.pdf",
                             "size": 1, "server_modified": "2026-01-01T00:00:00Z"}],
                "has_more": True,
                "cursor": "C1",
            }
            page1.raise_for_status = MagicMock()

            page2 = MagicMock()
            page2.json.return_value = {
                "entries": [{".tag": "file", "name": "b.pdf",
                             "path_lower": "/b.pdf",
                             "path_display": "/b.pdf",
                             "size": 2, "server_modified": "2026-01-01T00:00:00Z"}],
                "has_more": False,
            }
            page2.raise_for_status = MagicMock()

            with patch.object(dropbox_service, "_auth_header",
                              return_value={"Authorization": "Bearer X"}), \
                 patch.object(dropbox_service, "_get_token", return_value="TOK"), \
                 patch("app.services.dropbox_service.requests.post",
                       side_effect=[cursor_resp, page1, page2]), \
                 patch("app.services.cloud_sync_utils.parallel_download_then_index") as mock_idx, \
                 patch("app.ext.db.session.commit"):
                dropbox_service._full_sync(conn, {"errors": 0})

        assert conn.sync_cursor == "C0"
        # Both files queued for indexing
        assert mock_idx.called
        batch = mock_idx.call_args.args[0]
        assert {b["name"] for b in batch} == {"a.pdf", "b.pdf"}


# ─── _delta_sync ──────────────────────────────────────────────────────

class TestDeltaSync:
    def test_handles_deleted_and_file_entries(self, app):
        from app.services import dropbox_service
        with app.app_context():
            conn = MagicMock(spec=[
                "id", "folder_id", "sync_cursor",
                "access_token_enc", "refresh_token_enc", "token_expiry",
            ])
            conn.id = 1
            conn.folder_id = ""
            conn.sync_cursor = "C-prev"

            page = MagicMock()
            page.json.return_value = {
                "entries": [
                    {".tag": "deleted", "path_lower": "/old.pdf"},
                    {".tag": "file", "name": "new.pdf",
                     "path_lower": "/new.pdf",
                     "path_display": "/New.pdf",
                     "size": 1, "server_modified": "2026-01-01T00:00:00Z"},
                ],
                "has_more": False,
                "cursor": "C-next",
            }
            page.raise_for_status = MagicMock()

            with patch.object(dropbox_service, "_auth_header",
                              return_value={"Authorization": "Bearer X"}), \
                 patch.object(dropbox_service, "_get_token", return_value="TOK"), \
                 patch("app.services.dropbox_service.requests.post",
                       return_value=page), \
                 patch("app.services.cloud_sync_utils.parallel_download_then_index"), \
                 patch("app.services.dropbox_service.remove_cloud_doc") as mock_remove, \
                 patch("app.models.cloud_connection.CloudDocument") as mock_cd, \
                 patch("app.ext.db.session.commit"):
                # Perf audit H6: _delta_sync now pre-loads existing rows
                # in one IN query (.filter().all()) keyed on remote_id.
                # Stub returns the deleted-path row only — the new file
                # has no existing row.
                deleted_row = MagicMock()
                deleted_row.remote_id = "/old.pdf"
                mock_cd.query.filter.return_value.all.return_value = [deleted_row]
                stats = {"indexed": 0, "skipped": 0, "deleted": 0, "errors": 0}
                dropbox_service._delta_sync(conn, stats)

        assert stats["deleted"] == 1
        assert mock_remove.called
        assert conn.sync_cursor == "C-next"
