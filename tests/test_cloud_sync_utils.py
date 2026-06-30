"""Coverage for app/services/cloud_sync_utils.py — shared cloud-sync helpers."""
import uuid
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────
# mime_from_ext / is_indexable
# ─────────────────────────────────────────────────────────────────

class TestMimeFromExt:
    @pytest.mark.parametrize("filename,expected", [
        ("doc.pdf", "application/pdf"),
        ("memo.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("note.txt", "text/plain"),
        ("readme.md", "text/markdown"),
        ("doc.odt", "application/vnd.oasis.opendocument.text"),
        ("legacy.doc", "application/msword"),
        # Case-insensitive
        ("DOC.PDF", "application/pdf"),
    ])
    def test_known_extensions(self, filename, expected):
        from app.services.cloud_sync_utils import mime_from_ext
        assert mime_from_ext(filename) == expected

    @pytest.mark.parametrize("filename", ["image.png", "binary.exe", "noext", "data.json"])
    def test_unknown_extension_returns_none(self, filename):
        from app.services.cloud_sync_utils import mime_from_ext
        assert mime_from_ext(filename) is None


class TestIsIndexable:
    def test_known_mimes(self):
        from app.services.cloud_sync_utils import is_indexable
        assert is_indexable("application/pdf") is True
        assert is_indexable("text/plain") is True
        assert is_indexable("text/markdown") is True

    def test_unknown_mimes(self):
        from app.services.cloud_sync_utils import is_indexable
        assert is_indexable("image/png") is False
        assert is_indexable("application/octet-stream") is False
        assert is_indexable("") is False


# ─────────────────────────────────────────────────────────────────
# has_failed_docs
# ─────────────────────────────────────────────────────────────────

class TestHasFailedDocs:
    def test_returns_false_when_no_docs(self, app, db):
        from app.services.cloud_sync_utils import has_failed_docs
        with app.app_context():
            assert has_failed_docs(connection_id=99999) is False

    def test_returns_true_when_doc_has_zero_chunks(self, app, db):
        from app.models.cloud_connection import CloudConnection, CloudDocument
        from app.models.user import User
        from app.services.cloud_sync_utils import has_failed_docs

        with app.app_context():
            u = User(email=f"hf-{uuid.uuid4().hex[:6]}@example.com",
                    is_confirmed=True, is_active=True)
            u.set_password("Pass1234!")
            db.session.add(u)
            db.session.flush()
            c = CloudConnection(
                user_id=u.id, provider="gdrive", folder_id=None,
                access_token_enc="x", refresh_token_enc="x",
            )
            db.session.add(c)
            db.session.flush()

            doc = CloudDocument(
                connection_id=c.id, user_id=u.id,
                remote_id="failed-1", name="x.pdf",
                mime_type="application/pdf", file_size=100, chunk_count=0,
            )
            db.session.add(doc)
            db.session.commit()

            assert has_failed_docs(connection_id=c.id) is True

            db.session.delete(doc)
            db.session.delete(c)
            db.session.delete(u)
            db.session.commit()


# ─────────────────────────────────────────────────────────────────
# parallel_download_then_index — pool fan-out
# ─────────────────────────────────────────────────────────────────

class TestParallelDownloadThenIndex:
    def test_empty_list_is_noop(self):
        from app.services.cloud_sync_utils import parallel_download_then_index

        # Should not raise on empty list
        parallel_download_then_index([])

    def test_each_download_fn_called_once(self):
        """The pool must call download_fn once per item (no skips, no doubles)."""
        from app.services.cloud_sync_utils import parallel_download_then_index

        call_log = []

        def make_dl(label):
            def dl():
                call_log.append(label)
                return b"content-" + label.encode()
            return dl

        items = [
            {
                "conn": MagicMock(user_id=1, id=1),
                "remote_id": f"id-{i}",
                "name": f"f{i}.txt",
                "mime": "text/plain",
                "size": 10,
                "modified_at": None,
                "download_fn": make_dl(f"item{i}"),
                "ext": ".txt",
                "stats": {"indexed": 0, "skipped": 0, "errors": 0, "deleted": 0},
                "existing": None,
            }
            for i in range(3)
        ]

        with patch("app.services.cloud_sync_utils.process_and_index") as pai:
            parallel_download_then_index(items)

        # Each download_fn invoked exactly once
        assert sorted(call_log) == ["item0", "item1", "item2"]
        # process_and_index called once per item with preloaded_content
        assert pai.call_count == 3
        for call in pai.call_args_list:
            assert "preloaded_content" in call.kwargs
            assert call.kwargs["preloaded_content"].startswith(b"content-")

    def test_download_failure_marks_error_and_continues(self):
        from app.services.cloud_sync_utils import parallel_download_then_index

        good_dl = MagicMock(return_value=b"ok")
        bad_dl = MagicMock(side_effect=RuntimeError("network fail"))

        stats_good = {"indexed": 0, "skipped": 0, "errors": 0, "deleted": 0}
        stats_bad = {"indexed": 0, "skipped": 0, "errors": 0, "deleted": 0}

        items = [
            {
                "conn": MagicMock(), "remote_id": "ok",
                "name": "good.txt", "mime": "text/plain", "size": 1,
                "modified_at": None, "download_fn": good_dl, "ext": ".txt",
                "stats": stats_good, "existing": None,
            },
            {
                "conn": MagicMock(), "remote_id": "ko",
                "name": "bad.txt", "mime": "text/plain", "size": 1,
                "modified_at": None, "download_fn": bad_dl, "ext": ".txt",
                "stats": stats_bad, "existing": None,
            },
        ]

        with patch("app.services.cloud_sync_utils.process_and_index") as pai:
            parallel_download_then_index(items)

        assert stats_bad["errors"] == 1
        # The good item still went through process_and_index
        assert pai.call_count == 1

    def test_index_failure_marks_error(self):
        from app.services.cloud_sync_utils import parallel_download_then_index

        stats = {"indexed": 0, "skipped": 0, "errors": 0, "deleted": 0}
        items = [{
            "conn": MagicMock(), "remote_id": "x",
            "name": "x.txt", "mime": "text/plain", "size": 1,
            "modified_at": None,
            "download_fn": lambda: b"data",
            "ext": ".txt",
            "stats": stats, "existing": None,
        }]
        with patch(
            "app.services.cloud_sync_utils.process_and_index",
            side_effect=RuntimeError("index boom"),
        ):
            parallel_download_then_index(items)
        assert stats["errors"] == 1


# ─────────────────────────────────────────────────────────────────
# process_and_index — early-skip branches
# ─────────────────────────────────────────────────────────────────

class TestProcessAndIndexSkipBranches:
    def _make_user(self, app, db):
        from app.models.user import User
        with app.app_context():
            u = User(email=f"pai-{uuid.uuid4().hex[:6]}@example.com",
                    is_confirmed=True, is_active=True)
            u.set_password("Pass1234!")
            db.session.add(u)
            db.session.commit()
            return u.id

    def _make_conn(self, app, db, user_id):
        from app.models.cloud_connection import CloudConnection
        with app.app_context():
            c = CloudConnection(
                user_id=user_id, provider="gdrive", folder_id=None,
                access_token_enc="x", refresh_token_enc="x",
            )
            db.session.add(c)
            db.session.commit()
            return c.id

    def test_too_large_for_plan_marks_skipped(self, app, db):
        from app.models.cloud_connection import CloudConnection
        from app.models.user import User
        from app.services.cloud_sync_utils import process_and_index

        user_id = self._make_user(app, db)
        conn_id = self._make_conn(app, db, user_id)

        with app.app_context():
            conn = db.session.get(CloudConnection, conn_id)
            stats = {"indexed": 0, "skipped": 0, "errors": 0, "deleted": 0}
            # Per-file cap is 100 MB → 101 MB triggers the skip
            dl = MagicMock(return_value=b"data")
            process_and_index(
                conn=conn, remote_id="too-big", name="big.pdf",
                mime="application/pdf", size=101 * 1024 * 1024,
                modified_at=None, download_fn=dl, ext=".pdf",
                stats=stats, existing=None,
            )
            assert stats["skipped"] == 1
            assert stats.get("skipped_too_large") == 1
            # Download_fn not invoked (size check fires before)
            dl.assert_not_called()

            db.session.delete(db.session.get(CloudConnection, conn_id))
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()

    def test_unchanged_file_marks_skipped(self, app, db):
        from datetime import datetime

        from app.models.cloud_connection import CloudConnection, CloudDocument
        from app.models.user import User
        from app.services.cloud_sync_utils import process_and_index

        user_id = self._make_user(app, db)
        conn_id = self._make_conn(app, db, user_id)
        modified = datetime(2024, 1, 1, 12, 0, 0)

        with app.app_context():
            conn = db.session.get(CloudConnection, conn_id)
            existing = CloudDocument(
                connection_id=conn.id, user_id=user_id,
                remote_id="same", name="x.txt", mime_type="text/plain",
                file_size=100, chunk_count=5,
                remote_modified_at=modified,
            )
            db.session.add(existing)
            db.session.commit()

            stats = {"indexed": 0, "skipped": 0, "errors": 0, "deleted": 0}
            dl = MagicMock(return_value=b"x")
            process_and_index(
                conn=conn, remote_id="same", name="x.txt",
                mime="text/plain", size=100,
                modified_at=modified, download_fn=dl, ext=".txt",
                stats=stats, existing=existing,
            )
            assert stats["skipped"] == 1
            dl.assert_not_called()

            db.session.delete(existing)
            db.session.delete(db.session.get(CloudConnection, conn_id))
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()


# ─────────────────────────────────────────────────────────────────
# remove_cloud_doc
# ─────────────────────────────────────────────────────────────────

class TestRemoveCloudDoc:
    def test_unindexed_doc_just_deleted(self, app, db):
        from app.models.cloud_connection import CloudConnection, CloudDocument
        from app.models.user import User
        from app.services.cloud_sync_utils import remove_cloud_doc

        with app.app_context():
            u = User(email=f"rm-{uuid.uuid4().hex[:6]}@example.com",
                    is_confirmed=True, is_active=True)
            u.set_password("Pass1234!")
            db.session.add(u)
            db.session.flush()

            c = CloudConnection(
                user_id=u.id, provider="gdrive", folder_id=None,
                access_token_enc="x", refresh_token_enc="x",
            )
            db.session.add(c)
            db.session.flush()

            doc = CloudDocument(
                connection_id=c.id, user_id=u.id,
                remote_id="rm-1", name="x.txt", mime_type="text/plain",
                file_size=10, chunk_count=0,
            )
            db.session.add(doc)
            db.session.commit()
            doc_id = doc.id

            with patch("app.services.rag_service.remove_cloud_document_from_index") as rm:
                remove_cloud_doc(doc)
            # No chunks → don't bother calling the index
            rm.assert_not_called()
            assert db.session.get(CloudDocument, doc_id) is None

            db.session.delete(c)
            db.session.delete(u)
            db.session.commit()

    def test_indexed_doc_calls_remove_from_index(self, app, db):
        from app.models.cloud_connection import CloudConnection, CloudDocument
        from app.models.user import User
        from app.services.cloud_sync_utils import remove_cloud_doc

        with app.app_context():
            u = User(email=f"rm2-{uuid.uuid4().hex[:6]}@example.com",
                    is_confirmed=True, is_active=True)
            u.set_password("Pass1234!")
            db.session.add(u)
            db.session.flush()

            c = CloudConnection(
                user_id=u.id, provider="gdrive", folder_id=None,
                access_token_enc="x", refresh_token_enc="x",
            )
            db.session.add(c)
            db.session.flush()

            doc = CloudDocument(
                connection_id=c.id, user_id=u.id,
                remote_id="rm-2", name="y.txt", mime_type="text/plain",
                file_size=10, chunk_count=7,
            )
            db.session.add(doc)
            db.session.commit()

            with patch("app.services.rag_service.remove_cloud_document_from_index") as rm:
                remove_cloud_doc(doc)
            rm.assert_called_once()

            db.session.delete(c)
            db.session.delete(u)
            db.session.commit()


# ─────────────────────────────────────────────────────────────────
# process_and_index — orchestration paths
# ─────────────────────────────────────────────────────────────────

class TestProcessAndIndex:
    def _setup_user_and_conn(self, app, db):
        from app.models.cloud_connection import CloudConnection
        from app.models.user import User

        u = User(email=f"pi-{uuid.uuid4().hex[:6]}@example.com",
                 is_confirmed=True, is_active=True)
        u.set_password("Pass1234!")
        db.session.add(u)
        db.session.flush()

        c = CloudConnection(
            user_id=u.id, provider="gdrive", folder_id=None,
            access_token_enc="x", refresh_token_enc="x",
        )
        db.session.add(c)
        db.session.commit()
        return u, c

    def test_skips_when_size_exceeds_plan_cap(self, app, db):
        from app.services.cloud_sync_utils import process_and_index
        with app.app_context():
            u, c = self._setup_user_and_conn(app, db)
            stats = {"skipped": 0, "errors": 0}
            # 5 GB > _HARD_MAX_BYTES — falls into the size-skip branch.
            process_and_index(
                conn=c, remote_id="big-1", name="huge.pdf",
                mime="application/pdf", size=5 * 1024**3,
                modified_at=None, download_fn=lambda: b"",
                ext=".pdf", stats=stats,
            )
        assert stats["skipped"] == 1
        assert stats.get("skipped_too_large") == 1

    def test_indexes_new_document(self, app, db):
        from app.models.cloud_connection import CloudDocument
        from app.services.cloud_sync_utils import process_and_index

        with app.app_context():
            u, c = self._setup_user_and_conn(app, db)
            stats = {"indexed": 0, "skipped": 0, "errors": 0}
            with patch("app.services.rag_service.index_cloud_document",
                       return_value=3):
                process_and_index(
                    conn=c, remote_id="new-1", name="paper.pdf",
                    mime="application/pdf", size=1000,
                    modified_at=None,
                    download_fn=lambda: b"%PDF-1.4 stub",
                    ext=".pdf", stats=stats,
                )
            row = CloudDocument.query.filter_by(
                connection_id=c.id, remote_id="new-1",
            ).first()
        assert stats["indexed"] == 1
        assert row is not None
        assert row.chunk_count == 3
        assert row.indexed_at is not None

    def test_skips_when_unchanged(self, app, db):
        from datetime import datetime as _dt

        from app.models.cloud_connection import CloudDocument
        from app.services.cloud_sync_utils import process_and_index

        with app.app_context():
            u, c = self._setup_user_and_conn(app, db)

            # Pre-existing row that matches the modified_at of the next call.
            row = CloudDocument(
                connection_id=c.id, user_id=u.id,
                remote_id="same-1", name="x.pdf", mime_type="application/pdf",
                file_size=100, chunk_count=4,
                remote_modified_at=_dt(2026, 1, 1),
            )
            db.session.add(row)
            db.session.commit()

            stats = {"indexed": 0, "skipped": 0, "errors": 0}
            with patch("app.services.rag_service.index_cloud_document") as mock_idx:
                process_and_index(
                    conn=c, remote_id="same-1", name="x.pdf",
                    mime="application/pdf", size=100,
                    modified_at=_dt(2026, 1, 1),
                    download_fn=lambda: b"unused",
                    ext=".pdf", stats=stats,
                )
        assert stats["skipped"] == 1
        mock_idx.assert_not_called()

    def test_records_error_on_download_failure(self, app, db):
        from app.services.cloud_sync_utils import process_and_index
        with app.app_context():
            u, c = self._setup_user_and_conn(app, db)

            def bad_download():
                raise RuntimeError("network down")

            stats = {"indexed": 0, "skipped": 0, "errors": 0}
            process_and_index(
                conn=c, remote_id="err-1", name="paper.pdf",
                mime="application/pdf", size=100,
                modified_at=None, download_fn=bad_download,
                ext=".pdf", stats=stats,
            )
        assert stats["errors"] == 1

    def test_uses_preloaded_content_when_provided(self, app, db):
        from app.services.cloud_sync_utils import process_and_index

        with app.app_context():
            u, c = self._setup_user_and_conn(app, db)
            calls = {"download": 0}

            def should_not_run():
                calls["download"] += 1
                return b""

            stats = {"indexed": 0, "skipped": 0, "errors": 0}
            with patch("app.services.rag_service.index_cloud_document",
                       return_value=2):
                process_and_index(
                    conn=c, remote_id="pre-1", name="paper.pdf",
                    mime="application/pdf", size=10,
                    modified_at=None, download_fn=should_not_run,
                    ext=".pdf", stats=stats,
                    preloaded_content=b"%PDF-stub",
                )
        assert calls["download"] == 0
        assert stats["indexed"] == 1


# ─────────────────────────────────────────────────────────────────
# parallel_download_then_index
# ─────────────────────────────────────────────────────────────────

class TestParallelDownloadThenIndex:
    def test_empty_batch_is_noop(self):
        from app.services.cloud_sync_utils import parallel_download_then_index

        # Must not raise even without an app context.
        parallel_download_then_index([])

    def test_dispatches_each_item_with_preloaded_content(self):
        from app.services import cloud_sync_utils

        items = [
            {
                "conn": MagicMock(), "remote_id": f"id-{i}", "name": f"n{i}.pdf",
                "mime": "application/pdf", "size": 1, "modified_at": None,
                "download_fn": (lambda i=i: f"BYTES-{i}".encode()),
                "ext": ".pdf", "stats": {"indexed": 0, "errors": 0},
            }
            for i in range(3)
        ]
        with patch.object(cloud_sync_utils, "process_and_index") as mock_pi:
            cloud_sync_utils.parallel_download_then_index(items)
        assert mock_pi.call_count == 3
        # Each call carries the preloaded bytes returned by its download_fn.
        seen = {kwargs["preloaded_content"] for _, kwargs
                in mock_pi.call_args_list}
        assert seen == {b"BYTES-0", b"BYTES-1", b"BYTES-2"}

    def test_records_download_error_per_item(self):
        from app.services import cloud_sync_utils

        stats = {"indexed": 0, "errors": 0}

        def bad():
            raise RuntimeError("net")

        items = [{
            "conn": MagicMock(), "remote_id": "x", "name": "n.pdf",
            "mime": "application/pdf", "size": 1, "modified_at": None,
            "download_fn": bad, "ext": ".pdf", "stats": stats,
        }]
        with patch.object(cloud_sync_utils, "process_and_index") as mock_pi:
            cloud_sync_utils.parallel_download_then_index(items)
        assert stats["errors"] == 1
        mock_pi.assert_not_called()
