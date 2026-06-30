"""Coverage for app/services/s3_service.py — S3 + S3-compatible sync.

boto3 is mocked everywhere so tests don't need AWS credentials.
"""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────
# store_credentials / _get_credentials
# ─────────────────────────────────────────────────────────────────

class TestStoreCredentials:
    def test_persists_encrypted_payload_and_endpoint(self, app):
        from app.services import s3_service

        with app.test_request_context():
            conn = MagicMock(spec=[
                "access_token_enc", "refresh_token_enc", "token_expiry",
                "folder_id", "sync_cursor",
            ])
            s3_service.store_credentials(
                conn, "AKIA...", "s3-secret-xyz",
                "https://s3.eu-west-1.amazonaws.com", "my-bucket",
            )
        # Payload encrypted (different from raw access key)
        assert "AKIA" not in conn.access_token_enc
        assert conn.folder_id == "https://s3.eu-west-1.amazonaws.com|my-bucket"
        assert conn.sync_cursor is None
        assert conn.refresh_token_enc is None


class TestGetCredentials:
    def test_round_trips_credentials_and_endpoint(self, app):
        from app.services import s3_service
        from app.utils.crypto import encrypt_token

        with app.test_request_context():
            payload = json.dumps({"access_key": "AKIA-x", "secret_key": "secret-y"})
            conn = MagicMock(
                access_token_enc=encrypt_token(payload),
                folder_id="https://s3.amazonaws.com|bucket-1",
            )
            ak, sk, ep, bucket = s3_service._get_credentials(conn)
        assert ak == "AKIA-x"
        assert sk == "secret-y"
        assert ep == "https://s3.amazonaws.com"
        assert bucket == "bucket-1"

    def test_handles_missing_endpoint_url(self, app):
        from app.services import s3_service
        from app.utils.crypto import encrypt_token

        with app.test_request_context():
            payload = json.dumps({"access_key": "k", "secret_key": "s"})
            conn = MagicMock(
                access_token_enc=encrypt_token(payload),
                folder_id="|just-bucket",
            )
            _, _, ep, bucket = s3_service._get_credentials(conn)
        # Empty endpoint becomes None (boto3 default region)
        assert ep is None
        assert bucket == "just-bucket"


# ─────────────────────────────────────────────────────────────────
# _build_client
# ─────────────────────────────────────────────────────────────────

class TestBuildClient:
    def test_amazonaws_endpoint_is_dropped(self, app):
        from app.services import s3_service
        from app.utils.crypto import encrypt_token

        with app.test_request_context():
            payload = json.dumps({"access_key": "k", "secret_key": "s"})
            conn = MagicMock(
                access_token_enc=encrypt_token(payload),
                folder_id="https://s3.amazonaws.com|bucket",
            )
            with patch("boto3.client") as bc:
                s3_service._build_client(conn)
        # endpoint_url not passed when default Amazon URL detected
        kwargs = bc.call_args.kwargs
        assert "endpoint_url" not in kwargs

    def test_custom_endpoint_passed_through(self, app):
        from app.services import s3_service
        from app.utils.crypto import encrypt_token

        # Endpoint Scaleway moderne (vrai pattern de prod, pas le legacy
        # s3.scaleway.com). Doit matcher la whitelist _S3_ALLOWED_HOSTS_REGEX
        # ajoutée au fix bug C3 (sinon endpoints arbitraires = SSRF).
        with app.test_request_context():
            payload = json.dumps({"access_key": "k", "secret_key": "s"})
            conn = MagicMock(
                access_token_enc=encrypt_token(payload),
                folder_id="https://s3.fr-par.scw.cloud|bucket",
            )
            with patch("boto3.client") as bc:
                s3_service._build_client(conn)
        kwargs = bc.call_args.kwargs
        assert kwargs["endpoint_url"] == "https://s3.fr-par.scw.cloud"


# ─────────────────────────────────────────────────────────────────
# test_connection
# ─────────────────────────────────────────────────────────────────

class TestS3TestConnection:
    def test_calls_head_bucket(self):
        from app.services import s3_service
        client = MagicMock()
        with patch("boto3.client", return_value=client):
            s3_service.test_connection("AKIA", "secret", "https://s3.amazonaws.com", "b")
        client.head_bucket.assert_called_once_with(Bucket="b")

    def test_propagates_botocore_error(self):
        from app.services import s3_service
        client = MagicMock()
        client.head_bucket.side_effect = RuntimeError("403 Forbidden")
        with patch("boto3.client", return_value=client):
            with pytest.raises(RuntimeError, match="403"):
                s3_service.test_connection("k", "s", "https://s3.amazonaws.com", "b")


# ─────────────────────────────────────────────────────────────────
# _list_objects
# ─────────────────────────────────────────────────────────────────

class TestListObjects:
    def test_filters_indexable_objects(self):
        from app.services import s3_service

        client = MagicMock()
        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.return_value = iter([
            {
                "Contents": [
                    {"Key": "doc.pdf", "Size": 1, "ETag": '"abc"'},
                    {"Key": "image.png", "Size": 2, "ETag": '"def"'},
                    {"Key": "folder/", "Size": 0, "ETag": '"ghi"'},  # pseudo-folder
                    {"Key": "binary.exe", "Size": 5, "ETag": '"jkl"'},
                    {"Key": "note.md", "Size": 3, "ETag": '"mno"'},
                ],
            },
        ])
        out = s3_service._list_objects(client, "bucket", prefix="")
        keys = [o["Key"] for o in out]
        assert "doc.pdf" in keys
        assert "note.md" in keys
        # PNG / EXE / pseudo-folder excluded
        assert "image.png" not in keys
        assert "folder/" not in keys
        assert "binary.exe" not in keys
        # _mime was injected
        for o in out:
            assert "_mime" in o

    def test_passes_prefix_to_paginator(self):
        from app.services import s3_service

        client = MagicMock()
        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.return_value = iter([{"Contents": []}])
        s3_service._list_objects(client, "bucket", prefix="docs/")

        # paginator called with Prefix
        called_kwargs = paginator.paginate.call_args.kwargs
        assert called_kwargs.get("Prefix") == "docs/"
        assert called_kwargs.get("Bucket") == "bucket"

    def test_no_prefix_omitted_from_call(self):
        from app.services import s3_service

        client = MagicMock()
        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.return_value = iter([{"Contents": []}])
        s3_service._list_objects(client, "bucket", prefix="")
        called_kwargs = paginator.paginate.call_args.kwargs
        assert "Prefix" not in called_kwargs


# ─────────────────────────────────────────────────────────────────
# sync_connection — main state machine
# ─────────────────────────────────────────────────────────────────

class TestSyncConnection:
    def _create_s3_conn(self, app, db, *, sync_cursor=None, folder_name=""):
        from app.models.cloud_connection import CloudConnection
        from app.models.user import User
        from app.utils.crypto import encrypt_token
        with app.app_context():
            u = User(email=f"s3-{uuid.uuid4().hex[:6]}@example.com",
                     is_confirmed=True, is_active=True)
            u.set_password("Pass1234!")
            db.session.add(u)
            db.session.flush()
            uid = u.id
            payload = json.dumps({"access_key": "AKIA", "secret_key": "secret"})
            c = CloudConnection(
                user_id=uid, provider="s3",
                access_token_enc=encrypt_token(payload),
                refresh_token_enc=None,
                folder_id="https://s3.amazonaws.com|test-bucket",
                folder_name=folder_name,
                sync_cursor=sync_cursor,
            )
            db.session.add(c)
            db.session.commit()
            return uid, c.id

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

    def test_unknown_connection_returns_error(self, app, db):
        from app.services import s3_service
        with app.app_context():
            result = s3_service.sync_connection(99999)
        assert result.get("error") == "Connection not found"

    def test_missing_bucket_marks_error(self, app, db):
        from app.models.cloud_connection import CloudConnection
        from app.models.user import User
        from app.services import s3_service
        from app.utils.crypto import encrypt_token

        with app.app_context():
            u = User(email=f"s3b-{uuid.uuid4().hex[:6]}@example.com",
                     is_confirmed=True, is_active=True)
            u.set_password("Pass1234!")
            db.session.add(u)
            db.session.flush()
            uid = u.id
            payload = json.dumps({"access_key": "k", "secret_key": "s"})
            c = CloudConnection(
                user_id=uid, provider="s3",
                access_token_enc=encrypt_token(payload),
                folder_id="https://s3.amazonaws.com|",  # empty bucket
            )
            db.session.add(c)
            db.session.commit()
            cid = c.id

        with app.app_context():
            stats = s3_service.sync_connection(cid)
        assert stats["errors"] >= 1

        self._cleanup(app, db, uid, cid)

    def test_full_sync_calls_process_and_index_per_object(self, app, db):
        from app.services import s3_service

        uid, cid = self._create_s3_conn(app, db)

        client = MagicMock()
        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.return_value = iter([
            {
                "Contents": [
                    {"Key": "a.pdf", "Size": 100, "ETag": '"e1"',
                     "LastModified": datetime.now(timezone.utc)},
                    {"Key": "b.txt", "Size": 50, "ETag": '"e2"',
                     "LastModified": datetime.now(timezone.utc)},
                ],
            },
        ])
        with patch("boto3.client", return_value=client):
            with patch.object(s3_service, "process_and_index") as pai:
                with app.app_context():
                    stats = s3_service.sync_connection(cid)
        assert pai.call_count == 2
        assert stats["errors"] == 0

        self._cleanup(app, db, uid, cid)

    def test_delta_skips_unchanged_etag(self, app, db):
        from app.models.cloud_connection import CloudConnection, CloudDocument
        from app.services import s3_service

        # Connection with a prior cursor referencing one object
        cursor = json.dumps({
            "etags": {"a.pdf": "e1"},
            "last_sync": "2025-01-01T00:00:00",
        })
        uid, cid = self._create_s3_conn(app, db, sync_cursor=cursor)

        # Existing CloudDocument with chunk_count > 0
        with app.app_context():
            doc = CloudDocument(
                connection_id=cid, user_id=uid,
                remote_id="a.pdf", name="a.pdf",
                mime_type="application/pdf", file_size=100,
                chunk_count=5,
            )
            db.session.add(doc)
            db.session.commit()
            doc_id = doc.id

        client = MagicMock()
        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.return_value = iter([
            {
                "Contents": [
                    {"Key": "a.pdf", "Size": 100, "ETag": '"e1"',
                     "LastModified": datetime.now(timezone.utc)},
                ],
            },
        ])
        with patch("boto3.client", return_value=client):
            with patch.object(s3_service, "process_and_index") as pai:
                with app.app_context():
                    stats = s3_service.sync_connection(cid)

        # Unchanged ETag → skipped, process_and_index NOT called
        pai.assert_not_called()
        assert stats["skipped"] == 1

        with app.app_context():
            d = db.session.get(CloudDocument, doc_id)
            if d:
                db.session.delete(d)
            db.session.commit()
        self._cleanup(app, db, uid, cid)

    def test_removed_objects_trigger_deletion(self, app, db):
        from app.models.cloud_connection import CloudConnection, CloudDocument
        from app.services import s3_service

        # Cursor knows about a.pdf which is no longer in the bucket
        cursor = json.dumps({
            "etags": {"a.pdf": "e1"},
            "last_sync": "2025-01-01T00:00:00",
        })
        uid, cid = self._create_s3_conn(app, db, sync_cursor=cursor)

        with app.app_context():
            doc = CloudDocument(
                connection_id=cid, user_id=uid,
                remote_id="a.pdf", name="a.pdf",
                mime_type="application/pdf", file_size=10,
                chunk_count=3,
            )
            db.session.add(doc)
            db.session.commit()
            doc_id = doc.id

        client = MagicMock()
        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        # Bucket is empty now
        paginator.paginate.return_value = iter([{"Contents": []}])

        with patch("boto3.client", return_value=client):
            with patch("app.services.cloud_sync_utils.remove_cloud_doc") as rm:
                with app.app_context():
                    stats = s3_service.sync_connection(cid)

        assert stats["deleted"] == 1
        rm.assert_called_once()

        with app.app_context():
            d = db.session.get(CloudDocument, doc_id)
            if d:
                db.session.delete(d)
            db.session.commit()
        self._cleanup(app, db, uid, cid)

    def test_invalid_cursor_json_resets_to_empty(self, app, db):
        """Garbage in sync_cursor shouldn't crash — treat as fresh sync."""
        from app.services import s3_service

        uid, cid = self._create_s3_conn(app, db, sync_cursor="not-json{")

        client = MagicMock()
        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.return_value = iter([{"Contents": []}])

        with patch("boto3.client", return_value=client):
            with app.app_context():
                stats = s3_service.sync_connection(cid)
        assert stats["errors"] == 0

        self._cleanup(app, db, uid, cid)

    def test_persists_etag_cursor_after_sync(self, app, db):
        from app.models.cloud_connection import CloudConnection
        from app.services import s3_service

        uid, cid = self._create_s3_conn(app, db)

        client = MagicMock()
        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.return_value = iter([
            {
                "Contents": [
                    {"Key": "x.pdf", "Size": 1, "ETag": '"new-etag"',
                     "LastModified": datetime.now(timezone.utc)},
                ],
            },
        ])
        with patch("boto3.client", return_value=client):
            with patch.object(s3_service, "process_and_index"):
                with app.app_context():
                    s3_service.sync_connection(cid)

        with app.app_context():
            c_after = db.session.get(CloudConnection, cid)
            cursor = json.loads(c_after.sync_cursor)
            assert cursor["etags"].get("x.pdf") == "new-etag"
            assert "last_sync" in cursor

        self._cleanup(app, db, uid, cid)
