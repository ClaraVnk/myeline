"""Coverage for app/services/gdrive_service.py — Google Drive sync (service account).

googleapiclient and Drive API calls are mocked.
Tests focus on: indexable filtering, folder traversal, download dispatch,
polling sync state machine (new / updated / deleted files).
"""
import io
import json
import uuid
from unittest.mock import MagicMock, patch

import pytest


_SA_CREDENTIALS = json.dumps({
    "type": "service_account",
    "project_id": "test-proj",
    "private_key_id": "key-id",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA-----END RSA PRIVATE KEY-----\n",
    "client_email": "myeline-sync@test-proj.iam.gserviceaccount.com",
    "client_id": "123",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
})


# ─────────────────────────────────────────────────────────────────
# _is_indexable
# ─────────────────────────────────────────────────────────────────

class TestIsIndexable:
    @pytest.mark.parametrize("mime", [
        "application/pdf",
        "text/plain",
        "text/markdown",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.google-apps.document",
    ])
    def test_known_mime_types(self, mime):
        from app.services.gdrive_service import _is_indexable
        assert _is_indexable(mime) is True

    @pytest.mark.parametrize("mime", [
        "image/png", "video/mp4", "application/zip",
        "application/vnd.google-apps.folder",
        "application/vnd.google-apps.spreadsheet",
        "application/vnd.google-apps.presentation",
    ])
    def test_unknown_mime_types(self, mime):
        from app.services.gdrive_service import _is_indexable
        assert _is_indexable(mime) is False


# ─────────────────────────────────────────────────────────────────
# Service account helpers
# ─────────────────────────────────────────────────────────────────

class TestGetServiceAccountEmail:
    def test_returns_email_from_config(self, app):
        from app.services import gdrive_service
        with app.test_request_context():
            app.config["GOOGLE_SERVICE_ACCOUNT_CREDENTIALS"] = _SA_CREDENTIALS
            email = gdrive_service.get_service_account_email()
        assert email == "myeline-sync@test-proj.iam.gserviceaccount.com"

    def test_returns_none_when_not_configured(self, app):
        from app.services import gdrive_service
        with app.test_request_context():
            app.config["GOOGLE_SERVICE_ACCOUNT_CREDENTIALS"] = ""
            email = gdrive_service.get_service_account_email()
        assert email is None

    def test_returns_none_on_invalid_json(self, app):
        from app.services import gdrive_service
        with app.test_request_context():
            app.config["GOOGLE_SERVICE_ACCOUNT_CREDENTIALS"] = "not-json"
            email = gdrive_service.get_service_account_email()
        assert email is None


class TestExtractFolderId:
    def test_extracts_from_full_url(self):
        from app.services.gdrive_service import extract_folder_id
        url = "https://drive.google.com/drive/folders/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs"
        assert extract_folder_id(url) == "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs"

    def test_extracts_from_url_with_account_switcher(self):
        from app.services.gdrive_service import extract_folder_id
        url = "https://drive.google.com/drive/u/0/folders/ABC123xyz"
        assert extract_folder_id(url) == "ABC123xyz"

    def test_returns_raw_id_unchanged(self):
        from app.services.gdrive_service import extract_folder_id
        assert extract_folder_id("  ABC123xyz  ") == "ABC123xyz"


class TestValidateFolderAccess:
    def test_returns_metadata_for_valid_folder(self, app):
        from app.services import gdrive_service
        fake_service = MagicMock()
        fake_service.files().get().execute.return_value = {
            "id": "folder-id",
            "name": "My Research",
            "mimeType": "application/vnd.google-apps.folder",
        }
        with app.test_request_context():
            app.config["GOOGLE_SERVICE_ACCOUNT_CREDENTIALS"] = _SA_CREDENTIALS
            with patch.object(gdrive_service, "_build_service", return_value=fake_service):
                meta = gdrive_service.validate_folder_access("folder-id")
        assert meta["name"] == "My Research"

    def test_raises_when_not_a_folder(self, app):
        from app.services import gdrive_service
        fake_service = MagicMock()
        fake_service.files().get().execute.return_value = {
            "id": "file-id",
            "name": "doc.pdf",
            "mimeType": "application/pdf",
        }
        with app.test_request_context():
            app.config["GOOGLE_SERVICE_ACCOUNT_CREDENTIALS"] = _SA_CREDENTIALS
            with patch.object(gdrive_service, "_build_service", return_value=fake_service):
                with pytest.raises(RuntimeError, match="n'est pas un dossier"):
                    gdrive_service.validate_folder_access("file-id")

    def test_raises_when_api_error(self, app):
        from app.services import gdrive_service
        fake_service = MagicMock()
        fake_service.files().get().execute.side_effect = Exception("403 Forbidden")
        with app.test_request_context():
            app.config["GOOGLE_SERVICE_ACCOUNT_CREDENTIALS"] = _SA_CREDENTIALS
            with patch.object(gdrive_service, "_build_service", return_value=fake_service):
                with pytest.raises(RuntimeError, match="Impossible d'accéder"):
                    gdrive_service.validate_folder_access("folder-id")


# ─────────────────────────────────────────────────────────────────
# sync_connection — main entry
# ─────────────────────────────────────────────────────────────────

class TestSyncConnection:
    def test_unknown_connection_returns_error(self, app, db):
        from app.services import gdrive_service
        with app.app_context():
            result = gdrive_service.sync_connection(99999)
        assert result.get("error") == "Connection not found"

    def _create_conn(self, app, db):
        from app.models.cloud_connection import CloudConnection
        from app.models.user import User

        with app.app_context():
            u = User(email=f"gd-{uuid.uuid4().hex[:6]}@example.com",
                     is_confirmed=True, is_active=True)
            u.set_password("Pass1234!")
            db.session.add(u)
            db.session.flush()
            c = CloudConnection(
                user_id=u.id, provider="gdrive",
                access_token_enc="", folder_id="folder-123",
            )
            db.session.add(c)
            db.session.commit()
            return u.id, c.id

    def _cleanup(self, app, db, user_id, conn_id):
        from app.models.cloud_connection import CloudConnection
        from app.models.user import User
        with app.app_context():
            c = db.session.get(CloudConnection, conn_id)
            if c:
                db.session.delete(c)
            u = db.session.get(User, user_id)
            if u:
                db.session.delete(u)
            db.session.commit()

    def test_sync_calls_polling_sync(self, app, db):
        from app.services import gdrive_service
        user_id, cid = self._create_conn(app, db)
        with patch.object(gdrive_service, "_build_service"):
            with patch.object(gdrive_service, "_polling_sync") as ps:
                with app.app_context():
                    gdrive_service.sync_connection(cid)
        ps.assert_called_once()
        self._cleanup(app, db, user_id, cid)

    def test_exception_increments_errors_and_resets_is_syncing(self, app, db):
        from app.models.cloud_connection import CloudConnection
        from app.services import gdrive_service
        user_id, cid = self._create_conn(app, db)
        with patch.object(gdrive_service, "_build_service",
                          side_effect=RuntimeError("SA not configured")):
            with app.app_context():
                stats = gdrive_service.sync_connection(cid)
        assert stats["errors"] >= 1
        with app.app_context():
            assert db.session.get(CloudConnection, cid).is_syncing is False
        self._cleanup(app, db, user_id, cid)


# ─────────────────────────────────────────────────────────────────
# _polling_sync — new / updated / deleted
# ─────────────────────────────────────────────────────────────────

class TestPollingSync:
    def _make_user_and_conn(self, app, db, folder_id="folder-abc"):
        from app.models.cloud_connection import CloudConnection
        from app.models.user import User

        u = User(
            email=f"ps-{uuid.uuid4().hex[:6]}@example.com",
            is_confirmed=True, is_active=True,
            abonnement_type="premium_pro",
        )
        u.set_password("Ps1234!")
        db.session.add(u)
        db.session.flush()
        c = CloudConnection(
            user_id=u.id, provider="gdrive",
            access_token_enc="", folder_id=folder_id,
        )
        db.session.add(c)
        db.session.commit()
        return u, c

    def test_new_files_are_indexed(self, app, db):
        from app.services import gdrive_service
        with app.app_context():
            u, c = self._make_user_and_conn(app, db)
            service = MagicMock()
            stats = {"indexed": 0, "skipped": 0, "deleted": 0, "errors": 0}
            remote_files = [
                {"id": "F1", "name": "a.pdf", "mimeType": "application/pdf",
                 "size": 100, "modifiedTime": "2026-01-01T00:00:00Z"},
            ]
            with patch.object(gdrive_service, "_list_files", return_value=remote_files):
                with patch.object(gdrive_service, "_process_files_parallel") as mock_proc:
                    with patch("app.services.cloud_sync_utils.start_sync_progress"):
                        gdrive_service._polling_sync(c, service, stats)
            mock_proc.assert_called_once()

    def test_deleted_files_are_removed(self, app, db):
        from app.models.cloud_connection import CloudDocument
        from app.services import gdrive_service
        with app.app_context():
            u, c = self._make_user_and_conn(app, db)
            # File in DB but not in Drive → should be deleted
            orphan = CloudDocument(
                connection_id=c.id, user_id=u.id,
                remote_id="GONE", name="gone.pdf",
                mime_type="application/pdf", file_size=100, chunk_count=2,
            )
            db.session.add(orphan)
            db.session.commit()

            service = MagicMock()
            stats = {"indexed": 0, "skipped": 0, "deleted": 0, "errors": 0}

            with patch.object(gdrive_service, "_list_files", return_value=[]):
                with patch.object(gdrive_service, "_remove_cloud_doc") as mock_rm:
                    with patch.object(gdrive_service, "_process_files_parallel"):
                        with patch("app.services.cloud_sync_utils.start_sync_progress"):
                            gdrive_service._polling_sync(c, service, stats)

            assert stats["deleted"] == 1
            mock_rm.assert_called_once()

    def test_no_folder_id_returns_empty(self, app, db):
        from app.services import gdrive_service
        with app.app_context():
            u, c = self._make_user_and_conn(app, db, folder_id=None)
            service = MagicMock()
            stats = {"indexed": 0, "skipped": 0, "deleted": 0, "errors": 0}
            with patch.object(gdrive_service, "_process_files_parallel") as mock_proc:
                with patch("app.services.cloud_sync_utils.start_sync_progress"):
                    gdrive_service._polling_sync(c, service, stats)
            # _list_files returns [] for no folder → nothing processed
            mock_proc.assert_called_once()
            args = mock_proc.call_args[0]
            assert args[2] == []  # files list is empty


# ─────────────────────────────────────────────────────────────────
# _list_files
# ─────────────────────────────────────────────────────────────────

class TestListFiles:
    def test_returns_empty_when_no_folder_id(self):
        from app.services import gdrive_service
        result = gdrive_service._list_files(MagicMock(), folder_id=None)
        assert result == []

    def test_filters_indexable_files(self):
        from app.services import gdrive_service

        service = MagicMock()
        service.files().list().execute.side_effect = [
            {
                "files": [
                    {"id": "f1", "name": "doc.pdf", "mimeType": "application/pdf"},
                    {"id": "f2", "name": "img.png", "mimeType": "image/png"},
                    {"id": "f3", "name": "Sub", "mimeType": "application/vnd.google-apps.folder"},
                ],
            },
            # Recursion into subfolder f3
            {"files": []},
        ]
        out = gdrive_service._list_files(service, folder_id="root-folder")
        assert len(out) == 1
        assert out[0]["id"] == "f1"

    def test_paginates_via_next_page_token(self):
        from app.services import gdrive_service

        service = MagicMock()
        service.files().list().execute.side_effect = [
            {
                "files": [{"id": "p1", "name": "a.pdf", "mimeType": "application/pdf"}],
                "nextPageToken": "tok-2",
            },
            {
                "files": [{"id": "p2", "name": "b.pdf", "mimeType": "application/pdf"}],
            },
        ]
        out = gdrive_service._list_files(service, folder_id="root-folder")
        assert len(out) == 2

    def test_does_not_filter_by_ownership(self):
        """Service account accesses shared files — no 'me in owners' filter."""
        from app.services import gdrive_service

        service = MagicMock()
        # Capture the actual query string sent to the API
        captured_queries = []
        original_list = service.files().list

        def capturing_list(**kwargs):
            captured_queries.append(kwargs.get("q", ""))
            mock = MagicMock()
            mock.execute.return_value = {"files": []}
            return mock

        service.files().list.side_effect = None
        service.files().list = capturing_list

        gdrive_service._list_files(service, folder_id="root-folder")
        assert all("owners" not in q for q in captured_queries)


# ─────────────────────────────────────────────────────────────────
# _process_files_parallel
# ─────────────────────────────────────────────────────────────────

class TestProcessFilesParallel:
    def test_empty_list_is_noop(self):
        from app.services import gdrive_service
        gdrive_service._process_files_parallel(
            conn=MagicMock(), service=MagicMock(), files=[],
            stats={"indexed": 0, "skipped": 0, "errors": 0, "deleted": 0},
        )

    def test_dispatches_each_file_through_process_file(self, app):
        from app.services import gdrive_service

        files = [
            {"id": "f1", "name": "a.pdf", "mimeType": "application/pdf"},
            {"id": "f2", "name": "b.pdf", "mimeType": "application/pdf"},
        ]
        stats = {"indexed": 0, "skipped": 0, "errors": 0, "deleted": 0}

        with app.app_context():
            with patch.object(gdrive_service, "_build_service",
                              return_value=MagicMock()):
                with patch.object(gdrive_service, "_download_file",
                                  return_value=(b"content", ".pdf")):
                    with patch.object(gdrive_service, "_process_file") as pf:
                        gdrive_service._process_files_parallel(
                            conn=MagicMock(), service=MagicMock(),
                            files=files, stats=stats,
                        )
        assert pf.call_count == 2

    def test_download_failure_marks_error(self, app):
        from app.services import gdrive_service

        files = [{"id": "fbad", "name": "bad.pdf", "mimeType": "application/pdf"}]
        stats = {"indexed": 0, "skipped": 0, "errors": 0, "deleted": 0}

        with app.app_context():
            with patch.object(gdrive_service, "_build_service",
                              return_value=MagicMock()):
                with patch.object(gdrive_service, "_download_file",
                                  side_effect=RuntimeError("403 forbidden")):
                    with patch.object(gdrive_service, "_process_file") as pf:
                        gdrive_service._process_files_parallel(
                            conn=MagicMock(), service=MagicMock(),
                            files=files, stats=stats,
                        )
        assert stats["errors"] == 1
        pf.assert_not_called()


# ─────────────────────────────────────────────────────────────────
# _download_file
# ─────────────────────────────────────────────────────────────────

class TestDownloadFile:
    def test_native_file_uses_get_media(self):
        from app.services import gdrive_service

        service = MagicMock()
        with patch("googleapiclient.http.MediaIoBaseDownload") as Dl:
            dl_instance = MagicMock()
            dl_instance.next_chunk.side_effect = [(None, False), (None, True)]
            Dl.return_value = dl_instance
            content, ext = gdrive_service._download_file(
                service, "fileid", "application/pdf",
            )
        assert ext == ".pdf"
        service.files().get_media.assert_called_with(fileId="fileid")

    def test_gdoc_uses_export_media(self):
        from app.services import gdrive_service

        service = MagicMock()
        with patch("googleapiclient.http.MediaIoBaseDownload") as Dl:
            dl_instance = MagicMock()
            dl_instance.next_chunk.side_effect = [(None, True)]
            Dl.return_value = dl_instance
            content, ext = gdrive_service._download_file(
                service, "docid", "application/vnd.google-apps.document",
            )
        assert ext == ".docx"
        service.files().export_media.assert_called_once()


# ─────────────────────────────────────────────────────────────────
# _process_file
# ─────────────────────────────────────────────────────────────────

class TestProcessFile:
    def _make_user_and_conn(self, app, db):
        from app.models.cloud_connection import CloudConnection
        from app.models.user import User

        u = User(
            email=f"gpf-{uuid.uuid4().hex[:6]}@example.com",
            firstname="G", lastname="P",
            is_confirmed=True, is_active=True,
            abonnement_type="premium_pro",
        )
        u.set_password("Gpf1234!")
        db.session.add(u)
        db.session.flush()
        c = CloudConnection(
            user_id=u.id, provider="gdrive",
            access_token_enc="", folder_id="folder-x",
        )
        db.session.add(c)
        db.session.commit()
        return u, c

    def test_skips_oversized_file(self, app, db):
        from app.services import gdrive_service
        with app.app_context():
            u, c = self._make_user_and_conn(app, db)
            stats = {"indexed": 0, "skipped": 0, "errors": 0}
            gdrive_service._process_file(
                c, MagicMock(),
                {"id": "big", "name": "huge.pdf",
                 "mimeType": "application/pdf",
                 "size": str(60 * 1024**2)},
                stats,
            )
        assert stats["skipped"] == 1

    def test_skips_unchanged_file(self, app, db):
        from datetime import datetime as _dt

        from app.models.cloud_connection import CloudDocument
        from app.services import gdrive_service
        with app.app_context():
            u, c = self._make_user_and_conn(app, db)
            modified = _dt(2026, 1, 1, 12, 0)
            existing = CloudDocument(
                connection_id=c.id, user_id=u.id,
                remote_id="x1", name="x.pdf", mime_type="application/pdf",
                file_size=100, chunk_count=4, remote_modified_at=modified,
            )
            db.session.add(existing)
            db.session.commit()
            stats = {"indexed": 0, "skipped": 0, "errors": 0}
            with patch.object(gdrive_service, "_download_file") as mock_dl:
                gdrive_service._process_file(
                    c, MagicMock(),
                    {"id": "x1", "name": "x.pdf",
                     "mimeType": "application/pdf", "size": 100,
                     "modifiedTime": "2026-01-01T12:00:00Z"},
                    stats, existing=existing,
                )
        assert stats["skipped"] == 1
        mock_dl.assert_not_called()

    def test_records_error_on_download_failure(self, app, db):
        from app.services import gdrive_service
        with app.app_context():
            u, c = self._make_user_and_conn(app, db)
            stats = {"indexed": 0, "skipped": 0, "errors": 0}
            with patch.object(gdrive_service, "_download_file",
                              side_effect=Exception("network")):
                gdrive_service._process_file(
                    c, MagicMock(),
                    {"id": "x", "name": "x.pdf",
                     "mimeType": "application/pdf", "size": 100},
                    stats,
                )
        assert stats["errors"] == 1

    def test_indexes_with_preloaded_content(self, app, db):
        from app.models.cloud_connection import CloudDocument
        from app.services import gdrive_service
        with app.app_context():
            u, c = self._make_user_and_conn(app, db)
            stats = {"indexed": 0, "skipped": 0, "errors": 0}
            with patch("app.services.rag_service.index_cloud_document", return_value=5):
                gdrive_service._process_file(
                    c, MagicMock(),
                    {"id": "newpdf", "name": "n.pdf",
                     "mimeType": "application/pdf", "size": 200,
                     "webViewLink": "https://drive/n",
                     "modifiedTime": "2026-01-01T00:00:00Z"},
                    stats, content_and_ext=(b"%PDF-1.4 stub", ".pdf"),
                )
            row = CloudDocument.query.filter_by(
                connection_id=c.id, remote_id="newpdf",
            ).first()
        assert stats["indexed"] == 1
        assert row.chunk_count == 5
        assert row.web_url == "https://drive/n"

    def test_zero_chunks_marked_as_skipped(self, app, db):
        from app.services import gdrive_service
        with app.app_context():
            u, c = self._make_user_and_conn(app, db)
            stats = {"indexed": 0, "skipped": 0, "errors": 0}
            with patch("app.services.rag_service.index_cloud_document", return_value=0):
                gdrive_service._process_file(
                    c, MagicMock(),
                    {"id": "blank", "name": "b.pdf",
                     "mimeType": "application/pdf", "size": 200,
                     "modifiedTime": "2026-01-01T00:00:00Z"},
                    stats, content_and_ext=(b"unused", ".pdf"),
                )
        assert stats["skipped"] == 1
        assert stats["indexed"] == 0

    def test_handles_invalid_modified_time(self, app, db):
        from app.services import gdrive_service
        with app.app_context():
            u, c = self._make_user_and_conn(app, db)
            stats = {"indexed": 0, "skipped": 0, "errors": 0}
            with patch("app.services.rag_service.index_cloud_document", return_value=2):
                gdrive_service._process_file(
                    c, MagicMock(),
                    {"id": "x", "name": "x.pdf",
                     "mimeType": "application/pdf", "size": 200,
                     "modifiedTime": "not-a-date"},
                    stats, content_and_ext=(b"stub", ".pdf"),
                )
        assert stats["indexed"] == 1


# ─────────────────────────────────────────────────────────────────
# _remove_cloud_doc
# ─────────────────────────────────────────────────────────────────

class TestRemoveCloudDoc:
    def test_calls_index_remover_when_chunks_present(self, app):
        from app.services import gdrive_service
        with app.app_context():
            doc = MagicMock(chunk_count=4, user_id=1, id=42)
            with patch("app.services.rag_service.remove_cloud_document_from_index") as mock_rm, \
                 patch("app.ext.db.session.delete"), \
                 patch("app.ext.db.session.commit"):
                gdrive_service._remove_cloud_doc(doc)
        mock_rm.assert_called_once_with(1, 42, 4)

    def test_skips_index_remover_when_no_chunks(self, app):
        from app.services import gdrive_service
        with app.app_context():
            doc = MagicMock(chunk_count=0, user_id=1, id=42)
            with patch("app.services.rag_service.remove_cloud_document_from_index") as mock_rm, \
                 patch("app.ext.db.session.delete"), \
                 patch("app.ext.db.session.commit"):
                gdrive_service._remove_cloud_doc(doc)
        mock_rm.assert_not_called()
