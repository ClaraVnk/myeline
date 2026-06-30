"""Coverage for app/services/zotero_service.py — Zotero Web API v3 connector.

HTTP boundary (`requests.get`) is mocked; pure helpers (parsing, text builder,
cursor predicate) run end-to-end.
"""
from unittest.mock import MagicMock, patch


# ─── pure helpers ────────────────────────────────────────────────────

class TestParseFolderId:
    def test_empty(self):
        from app.services.zotero_service import _parse_folder_id
        assert _parse_folder_id("") == ("", None)
        assert _parse_folder_id(None) == ("", None)

    def test_user_id_only(self):
        from app.services.zotero_service import _parse_folder_id
        assert _parse_folder_id("12345") == ("12345", None)

    def test_user_and_collection(self):
        from app.services.zotero_service import _parse_folder_id
        assert _parse_folder_id("12345:ABCD") == ("12345", "ABCD")


class TestIsVersionCursor:
    def test_none_is_false(self):
        from app.services.zotero_service import _is_version_cursor
        assert _is_version_cursor(None) is False

    def test_empty_is_false(self):
        from app.services.zotero_service import _is_version_cursor
        assert _is_version_cursor("") is False

    def test_iso_timestamp_is_false(self):
        from app.services.zotero_service import _is_version_cursor
        assert _is_version_cursor("2026-01-01T00:00:00") is False

    def test_numeric_is_true(self):
        from app.services.zotero_service import _is_version_cursor
        assert _is_version_cursor("42") is True
        assert _is_version_cursor("0") is True


# ─── credentials ──────────────────────────────────────────────────────

class TestCredentials:
    def test_store_and_retrieve(self, app):
        from app.services.zotero_service import _get_api_key, store_credentials
        with app.test_request_context():
            conn = MagicMock(spec=[
                "access_token_enc", "folder_id", "folder_name", "sync_cursor",
            ])
            store_credentials(conn, "ZOTKEY-123", "98765")
            assert _get_api_key(conn) == "ZOTKEY-123"
            assert conn.folder_id == "98765"
            assert conn.folder_name is None
            assert conn.sync_cursor is None

    def test_headers_includes_api_key(self, app):
        from app.services.zotero_service import _headers, store_credentials
        with app.test_request_context():
            conn = MagicMock(spec=[
                "access_token_enc", "folder_id", "folder_name", "sync_cursor",
            ])
            store_credentials(conn, "ZK", "1")
            h = _headers(conn)
        assert h["Zotero-API-Key"] == "ZK"
        assert h["Zotero-API-Version"] == "3"


# ─── _build_text ──────────────────────────────────────────────────────

class TestBuildText:
    def test_minimal_item(self):
        from app.services.zotero_service import _build_text
        assert _build_text({"title": "Hello"}).startswith("Hello")

    def test_creators_with_first_last(self):
        from app.services.zotero_service import _build_text
        text = _build_text({
            "title": "Paper",
            "creators": [{"firstName": "Ada", "lastName": "Lovelace"},
                         {"name": "Group A"}],
        })
        assert "Ada Lovelace" in text
        assert "Group A" in text

    def test_includes_metadata_fields(self):
        from app.services.zotero_service import _build_text
        text = _build_text({
            "title": "Paper",
            "date": "2026-01",
            "DOI": "10.1234/x",
            "abstractNote": "An abstract.",
            "tags": [{"tag": "ai"}, {"tag": "rag"}],
        })
        assert "2026-01" in text
        assert "10.1234/x" in text
        assert "An abstract." in text
        assert "ai" in text and "rag" in text

    def test_handles_empty_fields_gracefully(self):
        from app.services.zotero_service import _build_text

        # No title, no creators, no metadata → returns empty string.
        assert _build_text({}) == ""


# ─── list_folders ─────────────────────────────────────────────────────

class TestListFolders:
    def test_returns_empty_when_no_user_id(self, app):
        from app.services.zotero_service import list_folders
        with app.test_request_context():
            conn = MagicMock(folder_id="")
            assert list_folders(conn) == []

    def test_maps_zotero_collections(self, app):
        from app.services.zotero_service import list_folders, store_credentials
        with app.test_request_context():
            conn = MagicMock(spec=[
                "access_token_enc", "folder_id", "folder_name", "sync_cursor",
            ])
            store_credentials(conn, "K", "12345")

            fake_resp = MagicMock()
            fake_resp.json.return_value = [
                {"key": "AAAA", "data": {"name": "Research"}},
                {"key": "BBBB", "data": {"name": "Drafts"}},
            ]
            fake_resp.raise_for_status = MagicMock()
            with patch("app.services.zotero_service.requests.get",
                       return_value=fake_resp):
                folders = list_folders(conn)
        ids = {f["id"] for f in folders}
        names = {f["name"] for f in folders}
        assert ids == {"12345:AAAA", "12345:BBBB"}
        assert names == {"Research", "Drafts"}


# ─── _fetch_items / _fetch_deleted_keys ───────────────────────────────

class TestFetchItems:
    def test_excludes_attachments_and_notes(self):
        from app.services.zotero_service import _fetch_items

        # Single page, total = 1 indexable + 1 attachment.
        resp = MagicMock()
        resp.json.return_value = [
            {"data": {"itemType": "journalArticle", "title": "Paper"}, "key": "k1"},
            {"data": {"itemType": "attachment"}, "key": "k2"},
        ]
        resp.headers = {"Last-Modified-Version": "42", "Total-Results": "2"}
        resp.raise_for_status = MagicMock()
        with patch("app.services.zotero_service.requests.get", return_value=resp):
            items, version = _fetch_items("1", None, {}, since=None)
        assert version == 42
        # attachment excluded
        assert len(items) == 1
        assert items[0]["data"]["title"] == "Paper"

    def test_collection_scoped_url(self):
        from app.services.zotero_service import _fetch_items

        resp = MagicMock()
        resp.json.return_value = []
        resp.headers = {"Last-Modified-Version": "0", "Total-Results": "0"}
        resp.raise_for_status = MagicMock()
        with patch("app.services.zotero_service.requests.get",
                   return_value=resp) as mock_get:
            _fetch_items("12345", "ABCD", {}, since=None)
        url = mock_get.call_args.args[0]
        assert "/users/12345/collections/ABCD/items" in url

    def test_paginates(self):
        from app.services.zotero_service import _fetch_items

        # Page 1: 100 items (full page). Page 2: returns empty → loop breaks.
        page1 = MagicMock()
        page1.json.return_value = [
            {"data": {"itemType": "journalArticle"}, "key": f"k{i}"}
            for i in range(100)
        ]
        page1.headers = {"Last-Modified-Version": "10", "Total-Results": "150"}
        page1.raise_for_status = MagicMock()

        page2 = MagicMock()
        page2.json.return_value = [
            {"data": {"itemType": "journalArticle"}, "key": f"k{i}"}
            for i in range(100, 150)
        ]
        page2.headers = {"Total-Results": "150"}
        page2.raise_for_status = MagicMock()

        with patch("app.services.zotero_service.requests.get",
                   side_effect=[page1, page2]):
            items, _ = _fetch_items("1", None, {}, since=None)
        assert len(items) == 150


class TestFetchDeletedKeys:
    def test_returns_keys_on_success(self):
        from app.services.zotero_service import _fetch_deleted_keys
        resp = MagicMock()
        resp.json.return_value = {"items": ["k1", "k2"]}
        resp.raise_for_status = MagicMock()
        with patch("app.services.zotero_service.requests.get", return_value=resp):
            keys = _fetch_deleted_keys("1", {}, since="42")
        assert keys == ["k1", "k2"]

    def test_swallows_network_error(self):
        from app.services.zotero_service import _fetch_deleted_keys
        with patch("app.services.zotero_service.requests.get",
                   side_effect=Exception("net")):
            assert _fetch_deleted_keys("1", {}, since="42") == []


# ─── _download_attachment ─────────────────────────────────────────────

class TestDownloadAttachment:
    def test_returns_content_on_200(self):
        from app.services.zotero_service import _download_attachment
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"PDFDATA"
        resp.raise_for_status = MagicMock()
        with patch("app.services.zotero_service.requests.get", return_value=resp):
            assert _download_attachment("1", "k", "key") == b"PDFDATA"

    def test_returns_none_on_404(self):
        from app.services.zotero_service import _download_attachment
        resp = MagicMock()
        resp.status_code = 404
        with patch("app.services.zotero_service.requests.get", return_value=resp):
            assert _download_attachment("1", "k", "key") is None

    def test_returns_none_on_http_error(self):
        import requests as _r

        from app.services.zotero_service import _download_attachment
        resp = MagicMock()
        resp.status_code = 500
        resp.raise_for_status.side_effect = _r.HTTPError("boom")
        with patch("app.services.zotero_service.requests.get", return_value=resp):
            assert _download_attachment("1", "k", "key") is None


# ─── _find_attachment ─────────────────────────────────────────────────

class TestFindAttachment:
    def test_picks_pdf_first(self):
        from app.services.zotero_service import _find_attachment
        resp = MagicMock()
        resp.json.return_value = [
            {"key": "snap", "data": {"contentType": "text/html",
                                     "linkMode": "imported_url"}},
            {"key": "pdf",  "data": {"contentType": "application/pdf"}},
        ]
        resp.raise_for_status = MagicMock()
        with patch("app.services.zotero_service.requests.get", return_value=resp):
            att = _find_attachment("1", "k", {})
        assert att["key"] == "pdf"

    def test_falls_back_to_snapshot_when_no_pdf(self):
        from app.services.zotero_service import _find_attachment
        resp = MagicMock()
        resp.json.return_value = [
            {"key": "snap", "data": {"contentType": "text/html",
                                     "linkMode": "imported_url"}},
        ]
        resp.raise_for_status = MagicMock()
        with patch("app.services.zotero_service.requests.get", return_value=resp):
            att = _find_attachment("1", "k", {})
        assert att["key"] == "snap"

    def test_returns_none_on_error(self):
        from app.services.zotero_service import _find_attachment
        with patch("app.services.zotero_service.requests.get",
                   side_effect=Exception("net")):
            assert _find_attachment("1", "k", {}) is None


# ─── sync_connection dispatch ─────────────────────────────────────────

class TestSyncConnection:
    def test_returns_error_when_connection_missing(self, app):
        from app.services.zotero_service import sync_connection
        with app.app_context():
            with patch("app.ext.db.session.get", return_value=None):
                result = sync_connection(999_999)
        assert "error" in result

    def test_dispatches_to_full_sync_when_no_cursor(self, app, db):
        from app.services import zotero_service
        from app.services.zotero_service import sync_connection
        with app.app_context():
            conn = MagicMock(spec=[
                "id", "user_id", "is_syncing", "sync_cursor",
                "folder_id", "last_sync_at", "access_token_enc",
            ])
            conn.id = 1
            conn.user_id = 1
            conn.is_syncing = False
            conn.sync_cursor = None
            with patch("app.ext.db.session.get", return_value=conn), \
                 patch("app.ext.db.session.commit"), \
                 patch.object(zotero_service, "_full_sync") as mock_full, \
                 patch.object(zotero_service, "_delta_sync") as mock_delta:
                sync_connection(1)
        mock_full.assert_called_once()
        mock_delta.assert_not_called()

    def test_dispatches_to_delta_sync_when_version_cursor(self, app, db):
        from app.services import zotero_service
        from app.services.zotero_service import sync_connection
        with app.app_context():
            conn = MagicMock(spec=[
                "id", "user_id", "is_syncing", "sync_cursor",
                "folder_id", "last_sync_at", "access_token_enc",
            ])
            conn.id = 1
            conn.user_id = 1
            conn.is_syncing = False
            conn.sync_cursor = "100"
            with patch("app.ext.db.session.get", return_value=conn), \
                 patch("app.ext.db.session.commit"), \
                 patch("app.services.cloud_sync_utils.has_failed_docs",
                       return_value=False), \
                 patch.object(zotero_service, "_full_sync") as mock_full, \
                 patch.object(zotero_service, "_delta_sync") as mock_delta:
                sync_connection(1)
        mock_delta.assert_called_once()
        mock_full.assert_not_called()


# ─── _full_sync wiring ────────────────────────────────────────────────

class TestFullSync:
    def test_skips_when_no_user_id(self, app):
        from app.services import zotero_service
        with app.app_context():
            conn = MagicMock(folder_id="")
            with patch.object(zotero_service, "_fetch_items") as mock_fetch, \
                 patch("app.ext.db.session.commit"):
                zotero_service._full_sync(conn, {"errors": 0})
        mock_fetch.assert_not_called()

    def test_processes_each_item_and_updates_cursor(self, app):
        from app.services import zotero_service
        with app.app_context():
            conn = MagicMock(spec=[
                "id", "user_id", "folder_id", "sync_cursor",
                "access_token_enc",
            ])
            conn.folder_id = "12345"
            with patch.object(zotero_service, "_fetch_items",
                              return_value=([{"data": {"title": "x"}, "key": "k1"},
                                             {"data": {"title": "y"}, "key": "k2"}],
                                            42)), \
                 patch.object(zotero_service, "_process_item") as mock_proc, \
                 patch.object(zotero_service, "_get_api_key", return_value="K"), \
                 patch("app.ext.db.session.commit"):
                zotero_service._full_sync(conn, {"errors": 0})
        assert mock_proc.call_count == 2
        assert conn.sync_cursor == "42"


# ─── _delta_sync wiring ───────────────────────────────────────────────

class TestDeltaSync:
    def test_no_op_when_library_unchanged(self, app):
        from app.services import zotero_service
        with app.app_context():
            conn = MagicMock(spec=[
                "id", "user_id", "folder_id", "sync_cursor",
                "access_token_enc",
            ])
            conn.id = 1
            conn.folder_id = "12345"
            conn.sync_cursor = "10"
            with patch.object(zotero_service, "_fetch_items",
                              return_value=([], 10)), \
                 patch.object(zotero_service, "_process_item") as mock_proc, \
                 patch.object(zotero_service, "_get_api_key", return_value="K"), \
                 patch.object(zotero_service, "_fetch_deleted_keys",
                              return_value=[]), \
                 patch("app.ext.db.session.commit"):
                zotero_service._delta_sync(conn, {"deleted": 0, "errors": 0})
        mock_proc.assert_not_called()

    def test_processes_changed_items_and_advances_cursor(self, app):
        from app.services import zotero_service
        with app.app_context():
            conn = MagicMock(spec=[
                "id", "user_id", "folder_id", "sync_cursor",
                "access_token_enc",
            ])
            conn.id = 1
            conn.folder_id = "12345"
            conn.sync_cursor = "10"
            with patch.object(zotero_service, "_fetch_items",
                              return_value=([{"data": {"title": "x"}, "key": "k1"}],
                                            42)), \
                 patch.object(zotero_service, "_process_item") as mock_proc, \
                 patch.object(zotero_service, "_get_api_key", return_value="K"), \
                 patch.object(zotero_service, "_fetch_deleted_keys",
                              return_value=[]), \
                 patch("app.ext.db.session.commit"):
                zotero_service._delta_sync(conn, {"deleted": 0, "errors": 0})
        mock_proc.assert_called_once()
        assert conn.sync_cursor == "42"


# ─── _process_item — full path coverage ──────────────────────────────

class TestProcessItem:
    def _make_user_and_conn(self, app, db):
        import uuid

        from app.models.cloud_connection import CloudConnection
        from app.models.user import User
        from app.services.zotero_service import store_credentials
        u = User(
            email=f"zproc-{uuid.uuid4().hex[:6]}@example.com",
            firstname="Z", lastname="P",
            is_confirmed=True, is_active=True,
            abonnement_type="premium_pro",
        )
        u.set_password("Zproc1234!")
        db.session.add(u)
        db.session.flush()
        c = CloudConnection(
            user_id=u.id, provider="zotero",
            access_token_enc="x", refresh_token_enc=None,
            folder_id="98765",
        )
        db.session.add(c)
        db.session.flush()
        store_credentials(c, "ZK", "98765")
        db.session.commit()
        return u, c

    def test_returns_early_when_no_key(self, app, db):
        from app.services.zotero_service import _process_item
        with app.app_context():
            u, c = self._make_user_and_conn(app, db)
            stats = {"indexed": 0, "skipped": 0, "errors": 0}
            _process_item(c, {"data": {}}, stats)
        # No key → early return, nothing indexed/skipped
        assert stats == {"indexed": 0, "skipped": 0, "errors": 0}

    def test_indexes_metadata_when_no_attachment(self, app, db):
        from app.models.cloud_connection import CloudDocument
        from app.services import zotero_service
        with app.app_context():
            u, c = self._make_user_and_conn(app, db)
            stats = {"indexed": 0, "skipped": 0, "errors": 0}
            with patch.object(zotero_service, "_find_attachment",
                              return_value=None), \
                 patch("app.services.rag_service.index_cloud_document",
                       return_value=4):
                zotero_service._process_item(
                    c,
                    {
                        "key": "AAA111",
                        "data": {
                            "title": "Paper",
                            "abstractNote": "abstract.",
                            "dateModified": "2026-01-01T12:00:00Z",
                        },
                    },
                    stats,
                )
            row = CloudDocument.query.filter_by(
                connection_id=c.id, remote_id="AAA111",
            ).first()
        assert stats["indexed"] == 1
        assert row is not None
        assert row.chunk_count == 4
        assert row.mime_type == "text/plain"

    def test_skips_when_metadata_text_is_empty(self, app, db):
        from app.services import zotero_service
        with app.app_context():
            u, c = self._make_user_and_conn(app, db)
            stats = {"indexed": 0, "skipped": 0, "errors": 0}
            with patch.object(zotero_service, "_find_attachment",
                              return_value=None), \
                 patch.object(zotero_service, "_build_text", return_value=""):
                zotero_service._process_item(
                    c, {"key": "BBB", "data": {}}, stats,
                )
        assert stats["skipped"] == 1

    def test_indexes_pdf_attachment_when_available(self, app, db):
        from app.services import zotero_service
        with app.app_context():
            u, c = self._make_user_and_conn(app, db)
            stats = {"indexed": 0, "skipped": 0, "errors": 0}
            attachment = {
                "key": "ATT-1",
                "data": {"contentType": "application/pdf"},
            }
            with patch.object(zotero_service, "_find_attachment",
                              return_value=attachment), \
                 patch.object(zotero_service, "_download_attachment",
                              return_value=b"%PDF-1.4 stub"), \
                 patch("app.services.rag_service.index_cloud_document",
                       return_value=8):
                zotero_service._process_item(
                    c,
                    {"key": "P1", "data": {"title": "WithPDF"}},
                    stats,
                )
        assert stats["indexed"] == 1

    def test_records_zero_chunks_as_skip(self, app, db):
        """Indexer returns 0 chunks → counted as skipped, not indexed."""
        from app.services import zotero_service
        with app.app_context():
            u, c = self._make_user_and_conn(app, db)
            stats = {"indexed": 0, "skipped": 0, "errors": 0}
            with patch.object(zotero_service, "_find_attachment",
                              return_value=None), \
                 patch("app.services.rag_service.index_cloud_document",
                       return_value=0):
                zotero_service._process_item(
                    c,
                    {"key": "ZC", "data": {"title": "Empty"}},
                    stats,
                )
        assert stats["skipped"] == 1
        assert stats["indexed"] == 0

    def test_skips_when_existing_unchanged(self, app, db):
        from datetime import datetime as _dt

        from app.models.cloud_connection import CloudDocument
        from app.services import zotero_service
        with app.app_context():
            u, c = self._make_user_and_conn(app, db)
            same_modified = _dt(2026, 1, 1, 12, 0)
            row = CloudDocument(
                connection_id=c.id, user_id=u.id,
                remote_id="UNC", name="x", mime_type="text/plain",
                file_size=10, chunk_count=3,
                remote_modified_at=same_modified,
            )
            db.session.add(row)
            db.session.commit()

            stats = {"indexed": 0, "skipped": 0, "errors": 0}
            with patch("app.services.rag_service.index_cloud_document") as mock_idx:
                zotero_service._process_item(
                    c,
                    {"key": "UNC", "data": {
                        "title": "x",
                        "dateModified": "2026-01-01T12:00:00Z",
                    }},
                    stats,
                )
        assert stats["skipped"] == 1
        mock_idx.assert_not_called()

    def test_handles_invalid_dateModified(self, app, db):
        """Bad date string → modified_at stays None, item still indexes."""
        from app.services import zotero_service
        with app.app_context():
            u, c = self._make_user_and_conn(app, db)
            stats = {"indexed": 0, "skipped": 0, "errors": 0}
            with patch.object(zotero_service, "_find_attachment",
                              return_value=None), \
                 patch("app.services.rag_service.index_cloud_document",
                       return_value=2):
                zotero_service._process_item(
                    c,
                    {"key": "BAD", "data": {
                        "title": "T", "dateModified": "not-a-date",
                    }},
                    stats,
                )
        assert stats["indexed"] == 1

    def test_attachment_present_but_download_returns_none_falls_back_to_metadata(self, app, db):
        from app.services import zotero_service
        with app.app_context():
            u, c = self._make_user_and_conn(app, db)
            stats = {"indexed": 0, "skipped": 0, "errors": 0}
            attachment = {"key": "A", "data": {"contentType": "application/pdf"}}
            with patch.object(zotero_service, "_find_attachment",
                              return_value=attachment), \
                 patch.object(zotero_service, "_download_attachment",
                              return_value=None), \
                 patch("app.services.rag_service.index_cloud_document",
                       return_value=2):
                zotero_service._process_item(
                    c,
                    {"key": "FB", "data": {"title": "T", "abstractNote": "abs."}},
                    stats,
                )
        # Indexed — fell back to text path
        assert stats["indexed"] == 1
