"""Coverage for app/services/onedrive_service.py — Microsoft Graph API connector.

OAuth + Graph HTTP boundary mocked at `requests.get` / `requests.post`.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch


# ─── OAuth ────────────────────────────────────────────────────────────

class TestGetAuthUrl:
    def test_includes_client_id_and_offline_scope(self, app):
        from app.services.onedrive_service import get_auth_url
        with app.app_context():
            app.config["ONEDRIVE_CLIENT_ID"] = "cid"
            app.config["ONEDRIVE_CLIENT_SECRET"] = "csec"
            url, state = get_auth_url("https://example.com/cb")
        assert "client_id=cid" in url
        # _SCOPES is URL-encoded ('Files.Read offline_access' → '+' or '%20')
        assert "offline_access" in url
        assert state and len(state) > 16


class TestStoreTokens:
    def test_persists_tokens_and_expiry(self, app):
        from app.services.onedrive_service import store_tokens
        with app.app_context():
            app.config["ONEDRIVE_CLIENT_ID"] = "cid"
            app.config["ONEDRIVE_CLIENT_SECRET"] = "csec"
            conn = MagicMock(spec=[
                "access_token_enc", "refresh_token_enc", "token_expiry",
            ])
            fake_resp = MagicMock()
            fake_resp.json.return_value = {
                "access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
            }
            fake_resp.raise_for_status = MagicMock()
            with patch("app.services.onedrive_service.requests.post",
                       return_value=fake_resp):
                store_tokens(conn, "code", "https://ex/cb")
        # Encrypted columns must not equal the plaintext (we can't assert
        # absence of "AT" — it's a 2-letter substring that will collide
        # with random base64 output).
        assert conn.access_token_enc and conn.access_token_enc != "AT"
        assert conn.refresh_token_enc and conn.refresh_token_enc != "RT"
        assert conn.token_expiry is not None


# ─── _get_token ───────────────────────────────────────────────────────

class TestGetToken:
    def test_returns_cached_when_not_expired(self, app):
        from app.services.onedrive_service import _get_token
        from app.utils.crypto import encrypt_token
        with app.app_context():
            conn = MagicMock(spec=[
                "access_token_enc", "refresh_token_enc", "token_expiry",
            ])
            conn.access_token_enc = encrypt_token("CACHED")
            conn.token_expiry = (
                datetime.now(timezone.utc).replace(tzinfo=None)
                + timedelta(hours=1)
            )
            with patch("app.services.onedrive_service.requests.post") as mock_post:
                tok = _get_token(conn)
        assert tok == "CACHED"
        mock_post.assert_not_called()

    def test_refreshes_when_expired(self, app):
        from app.services.onedrive_service import _get_token
        from app.utils.crypto import encrypt_token
        with app.app_context():
            app.config["ONEDRIVE_CLIENT_ID"] = "c"
            app.config["ONEDRIVE_CLIENT_SECRET"] = "s"
            conn = MagicMock(spec=[
                "id", "access_token_enc", "refresh_token_enc", "token_expiry",
            ])
            conn.id = 42
            conn.access_token_enc = encrypt_token("OLD")
            conn.refresh_token_enc = encrypt_token("RT")
            conn.token_expiry = (
                datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(minutes=5)
            )
            fake_resp = MagicMock()
            fake_resp.json.return_value = {
                "access_token": "NEW", "expires_in": 3600,
            }
            fake_resp.raise_for_status = MagicMock()
            with patch("app.services.onedrive_service.requests.post",
                       return_value=fake_resp), \
                 patch("app.ext.db.session.commit"), \
                 patch("app.ext.db.session.refresh"):
                tok = _get_token(conn)
        assert tok == "NEW"

    def test_returns_static_when_no_refresh_token(self, app):
        from app.services.onedrive_service import _get_token
        from app.utils.crypto import encrypt_token
        with app.app_context():
            conn = MagicMock(spec=[
                "access_token_enc", "refresh_token_enc", "token_expiry",
            ])
            conn.access_token_enc = encrypt_token("STATIC")
            conn.refresh_token_enc = None
            conn.token_expiry = None
            assert _get_token(conn) == "STATIC"


# ─── list_folders ─────────────────────────────────────────────────────

class TestListFolders:
    def test_maps_graph_response(self, app):
        from app.services.onedrive_service import list_folders
        with app.app_context():
            conn = MagicMock()
            fake_resp = MagicMock()
            fake_resp.json.return_value = {"value": [
                {"id": "F1", "name": "Docs"},
                {"id": "F2", "name": "Photos"},
            ]}
            fake_resp.raise_for_status = MagicMock()
            with patch("app.services.onedrive_service._headers",
                       return_value={"Authorization": "Bearer X"}), \
                 patch("app.services.onedrive_service.requests.get",
                       return_value=fake_resp):
                folders = list_folders(conn)
        assert {f["id"] for f in folders} == {"F1", "F2"}


# ─── _delta_base_url ──────────────────────────────────────────────────

class TestDeltaBaseUrl:
    def test_root_when_no_folder(self):
        from app.services.onedrive_service import _delta_base_url
        conn = MagicMock(folder_id=None)
        assert _delta_base_url(conn).endswith("/me/drive/root/delta")

    def test_item_scoped_when_folder_set(self):
        from app.services.onedrive_service import _delta_base_url
        conn = MagicMock(folder_id="F-abc")
        url = _delta_base_url(conn)
        assert "items/F-abc/delta" in url


# ─── _build_index_descriptor ──────────────────────────────────────────

class TestBuildIndexDescriptor:
    def test_returns_none_for_unsupported(self, app):
        from app.services.onedrive_service import _build_index_descriptor
        with app.app_context():
            conn = MagicMock()
            item = {"id": "x", "name": "img.heic", "size": 1,
                    "file": {"mimeType": "image/heic"}}
            assert _build_index_descriptor(conn, item, {}) is None

    def test_returns_descriptor_for_pdf(self, app):
        from app.services import onedrive_service
        with app.app_context():
            conn = MagicMock()
            item = {"id": "x", "name": "notes.pdf", "size": 100,
                    "file": {"mimeType": "application/pdf"},
                    "lastModifiedDateTime": "2026-01-01T00:00:00Z",
                    "webUrl": "https://1drv.ms/x"}
            with patch.object(onedrive_service, "_get_token", return_value="TOK"):
                d = onedrive_service._build_index_descriptor(conn, item, {})
        assert d["mime"] == "application/pdf"
        assert d["web_url"] == "https://1drv.ms/x"
        assert callable(d["download_fn"])

    def test_falls_back_to_extension_mime(self, app):
        """When Graph reports no/unknown mimeType, fall back to extension."""
        from app.services import onedrive_service
        with app.app_context():
            conn = MagicMock()
            item = {"id": "x", "name": "notes.pdf", "size": 100,
                    "file": {"mimeType": "application/octet-stream"},
                    "lastModifiedDateTime": "2026-01-01T00:00:00Z"}
            with patch.object(onedrive_service, "_get_token", return_value="TOK"):
                d = onedrive_service._build_index_descriptor(conn, item, {})
        assert d is not None
        assert d["mime"] == "application/pdf"


# ─── sync_connection wiring ───────────────────────────────────────────

class TestSyncConnection:
    def test_returns_error_when_connection_missing(self, app):
        from app.services.onedrive_service import sync_connection
        with app.app_context():
            with patch("app.ext.db.session.get", return_value=None):
                result = sync_connection(999_999)
        assert "error" in result

    def test_dispatches_to_full_when_no_cursor(self, app, db):
        from app.services import onedrive_service
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
                 patch.object(onedrive_service, "_full_sync") as mock_full, \
                 patch.object(onedrive_service, "_delta_sync") as mock_delta:
                onedrive_service.sync_connection(1)
        mock_full.assert_called_once()
        mock_delta.assert_not_called()

    def test_dispatches_to_delta_when_cursor_set(self, app, db):
        from app.services import onedrive_service
        with app.app_context():
            conn = MagicMock(spec=[
                "id", "is_syncing", "sync_cursor", "folder_id",
                "last_sync_at",
            ])
            conn.id = 1
            conn.is_syncing = False
            conn.sync_cursor = "https://graph.microsoft.com/.../delta?token=x"
            with patch("app.ext.db.session.get", return_value=conn), \
                 patch("app.ext.db.session.commit"), \
                 patch("app.services.cloud_sync_utils.has_failed_docs",
                       return_value=False), \
                 patch.object(onedrive_service, "_full_sync") as mock_full, \
                 patch.object(onedrive_service, "_delta_sync") as mock_delta:
                onedrive_service.sync_connection(1)
        mock_delta.assert_called_once()
        mock_full.assert_not_called()


# ─── _full_sync ───────────────────────────────────────────────────────

class TestFullSync:
    def test_walks_paginated_delta_and_records_cursor(self, app):
        from app.services import onedrive_service
        with app.app_context():
            conn = MagicMock(spec=[
                "id", "folder_id", "sync_cursor",
                "access_token_enc", "refresh_token_enc", "token_expiry",
            ])
            conn.id = 1
            conn.folder_id = None

            page1 = MagicMock()
            page1.json.return_value = {
                "value": [
                    {"id": "i1", "name": "a.pdf", "size": 1,
                     "file": {"mimeType": "application/pdf"},
                     "lastModifiedDateTime": "2026-01-01T00:00:00Z",
                     "webUrl": "https://1drv.ms/a"},
                ],
                "@odata.nextLink": "https://graph/next",
            }
            page1.raise_for_status = MagicMock()

            page2 = MagicMock()
            page2.json.return_value = {
                "value": [
                    {"id": "i2", "name": "b.pdf", "size": 2,
                     "file": {"mimeType": "application/pdf"},
                     "lastModifiedDateTime": "2026-01-01T00:00:00Z",
                     "webUrl": "https://1drv.ms/b"},
                ],
                "@odata.deltaLink": "https://graph/delta-token-X",
            }
            page2.raise_for_status = MagicMock()

            with patch.object(onedrive_service, "_headers",
                              return_value={"Authorization": "Bearer X"}), \
                 patch.object(onedrive_service, "_get_token", return_value="TOK"), \
                 patch("app.services.onedrive_service.requests.get",
                       side_effect=[page1, page2]), \
                 patch("app.services.cloud_sync_utils.parallel_download_then_index") as mock_idx, \
                 patch("app.ext.db.session.commit"):
                onedrive_service._full_sync(conn, {"errors": 0})

        assert conn.sync_cursor == "https://graph/delta-token-X"
        assert mock_idx.called
        batch = mock_idx.call_args.args[0]
        assert {b["name"] for b in batch} == {"a.pdf", "b.pdf"}


# ─── _delta_sync ──────────────────────────────────────────────────────

class TestDeltaSync:
    def test_handles_deleted_and_file_entries(self, app):
        from app.services import onedrive_service
        with app.app_context():
            conn = MagicMock(spec=[
                "id", "folder_id", "sync_cursor",
                "access_token_enc", "refresh_token_enc", "token_expiry",
            ])
            conn.id = 1
            conn.sync_cursor = "https://graph/delta-prev"

            page = MagicMock()
            page.json.return_value = {
                "value": [
                    {"id": "old", "deleted": {"state": "deleted"}},
                    {"id": "new", "name": "n.pdf", "size": 5,
                     "file": {"mimeType": "application/pdf"},
                     "lastModifiedDateTime": "2026-01-01T00:00:00Z",
                     "webUrl": "https://1drv.ms/n"},
                ],
                "@odata.deltaLink": "https://graph/delta-next",
            }
            page.raise_for_status = MagicMock()

            with patch.object(onedrive_service, "_headers",
                              return_value={"Authorization": "Bearer X"}), \
                 patch.object(onedrive_service, "_get_token", return_value="TOK"), \
                 patch("app.services.onedrive_service.requests.get",
                       return_value=page), \
                 patch("app.services.onedrive_service.remove_cloud_doc") as mock_remove, \
                 patch("app.services.cloud_sync_utils.parallel_download_then_index"), \
                 patch("app.models.cloud_connection.CloudDocument") as mock_cd, \
                 patch("app.ext.db.session.commit"):
                # Perf audit H6: _delta_sync now pre-loads existing rows
                # in a single IN query (.filter().all()) keyed on
                # remote_id. Stub returns the deleted item only — the
                # new item has no existing row.
                deleted_row = MagicMock()
                deleted_row.remote_id = "old"
                mock_cd.query.filter.return_value.all.return_value = [deleted_row]
                stats = {"indexed": 0, "skipped": 0, "deleted": 0, "errors": 0}
                onedrive_service._delta_sync(conn, stats)

        assert stats["deleted"] == 1
        assert mock_remove.called
        assert conn.sync_cursor == "https://graph/delta-next"
