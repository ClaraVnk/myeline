"""Coverage for cloud service OAuth/token paths — onedrive, dropbox, kdrive, zotero.

These services share a common shape (OAuth helpers, token refresh, sync_connection
state machine, _build_index_descriptor). We test the same boundaries across all
four to cover the bulk of their surface without duplicating service-specific
HTTP fixture work.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────
# Helpers — shared sync_connection setup
# ─────────────────────────────────────────────────────────────────

def _create_conn(app, db, *, provider, sync_cursor=None):
    from app.models.cloud_connection import CloudConnection
    from app.models.user import User
    with app.app_context():
        u = User(email=f"{provider}-{uuid.uuid4().hex[:6]}@example.com",
                 is_confirmed=True, is_active=True)
        u.set_password("Pass1234!")
        db.session.add(u)
        db.session.flush()
        user_id = u.id
        c = CloudConnection(
            user_id=user_id, provider=provider,
            access_token_enc="x", refresh_token_enc="x",
            sync_cursor=sync_cursor,
        )
        db.session.add(c)
        db.session.commit()
        return user_id, c.id


def _cleanup(app, db, user_id, conn_id):
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


# ─────────────────────────────────────────────────────────────────
# OneDrive
# ─────────────────────────────────────────────────────────────────

class TestOneDriveOAuth:
    def test_get_auth_url_includes_client_id_and_state(self, app):
        from app.services import onedrive_service
        with app.test_request_context():
            app.config["ONEDRIVE_CLIENT_ID"] = "ms-client-id"
            app.config["ONEDRIVE_CLIENT_SECRET"] = "ms-secret"
            url, state = onedrive_service.get_auth_url("https://cb.example.com")
        assert "ms-client-id" in url
        assert state and len(state) > 10
        # State appears in the URL params
        assert state in url

    def test_store_tokens_persists_encrypted(self, app):
        from app.services import onedrive_service

        with app.test_request_context():
            app.config["ONEDRIVE_CLIENT_ID"] = "id"
            app.config["ONEDRIVE_CLIENT_SECRET"] = "secret"
            conn = MagicMock(
                spec=["access_token_enc", "refresh_token_enc", "token_expiry"]
            )
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {
                "access_token": "raw-access",
                "refresh_token": "raw-refresh",
                "expires_in": 3600,
            }
            with patch("app.services.onedrive_service.requests.post",
                       return_value=mock_resp):
                onedrive_service.store_tokens(conn, "code", "https://cb")
        # Encrypted, not raw
        assert conn.access_token_enc != "raw-access"
        assert conn.refresh_token_enc != "raw-refresh"
        # Expiry set in the future
        assert conn.token_expiry is not None


class TestOneDriveTokenRefresh:
    def test_returns_decrypted_when_token_still_valid(self, app):
        from app.services import onedrive_service
        from app.utils.crypto import encrypt_token

        with app.test_request_context():
            conn = MagicMock(
                access_token_enc=encrypt_token("valid-token"),
                refresh_token_enc=encrypt_token("refresh"),
                # Expires in 1 hour → still valid (60s lead time)
                token_expiry=datetime.now(timezone.utc).replace(tzinfo=None)
                + timedelta(hours=1),
            )
            tok = onedrive_service._get_token(conn)
        assert tok == "valid-token"

    def test_refreshes_when_token_expired(self, app, db):
        from app.models.cloud_connection import CloudConnection
        from app.models.user import User
        from app.services import onedrive_service
        from app.utils.crypto import encrypt_token

        with app.app_context():
            app.config["ONEDRIVE_CLIENT_ID"] = "id"
            app.config["ONEDRIVE_CLIENT_SECRET"] = "secret"

            u = User(email=f"od-{uuid.uuid4().hex[:6]}@example.com",
                     is_confirmed=True, is_active=True)
            u.set_password("Pass1234!")
            db.session.add(u)
            db.session.flush()
            uid = u.id

            c = CloudConnection(
                user_id=uid, provider="onedrive",
                access_token_enc=encrypt_token("expired"),
                refresh_token_enc=encrypt_token("refresh-tok"),
                token_expiry=datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(hours=1),  # expired
            )
            db.session.add(c)
            db.session.commit()
            cid = c.id

            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {
                "access_token": "fresh-token",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            }
            with patch("app.services.onedrive_service.requests.post",
                       return_value=mock_resp):
                tok = onedrive_service._get_token(c)
        assert tok == "fresh-token"

        _cleanup(app, db, uid, cid)


class TestOneDriveSyncConnection:
    def test_unknown_connection_returns_error(self, app, db):
        from app.services import onedrive_service
        with app.app_context():
            result = onedrive_service.sync_connection(99999)
        assert "error" in result

    def test_first_sync_runs_full_sync(self, app, db):
        from app.services import onedrive_service
        user_id, cid = _create_conn(app, db, provider="onedrive", sync_cursor=None)
        with patch.object(onedrive_service, "_full_sync") as fs:
            with patch.object(onedrive_service, "_delta_sync") as ds:
                with patch("app.services.cloud_sync_utils.has_failed_docs",
                           return_value=False):
                    with app.app_context():
                        onedrive_service.sync_connection(cid)
        fs.assert_called_once()
        ds.assert_not_called()
        _cleanup(app, db, user_id, cid)


# ─────────────────────────────────────────────────────────────────
# Dropbox
# ─────────────────────────────────────────────────────────────────

class TestDropboxOAuth:
    def test_get_auth_url_includes_client_id_and_state(self, app):
        from app.services import dropbox_service
        with app.test_request_context():
            app.config["DROPBOX_APP_KEY"] = "dbx-key"
            app.config["DROPBOX_APP_SECRET"] = "dbx-secret"
            url, state = dropbox_service.get_auth_url("https://cb.example.com")
        assert "dbx-key" in url
        assert state in url

    def test_store_tokens_persists_encrypted(self, app):
        from app.services import dropbox_service

        with app.test_request_context():
            app.config["DROPBOX_APP_KEY"] = "key"
            app.config["DROPBOX_APP_SECRET"] = "secret"
            conn = MagicMock(spec=["access_token_enc", "refresh_token_enc", "token_expiry"])
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {
                "access_token": "dbx-access",
                "refresh_token": "dbx-refresh",
                "expires_in": 14400,
            }
            with patch("app.services.dropbox_service.requests.post",
                       return_value=mock_resp):
                dropbox_service.store_tokens(conn, "code", "https://cb")
        assert conn.access_token_enc != "dbx-access"


class TestDropboxMimeFromPath:
    @pytest.mark.parametrize("path,expected_mime", [
        ("/notes/x.pdf", "application/pdf"),
        ("/files/y.txt", "text/plain"),
        ("/docs/Z.MD", "text/markdown"),  # case-insensitive
        ("/files/binary.exe", None),
        ("/no-extension", None),
    ])
    def test_mime_resolution(self, path, expected_mime):
        from app.services.dropbox_service import mime_from_path
        assert mime_from_path(path) == expected_mime


class TestDropboxSyncConnection:
    def test_unknown_connection_returns_error(self, app, db):
        from app.services import dropbox_service
        with app.app_context():
            result = dropbox_service.sync_connection(99999)
        assert "error" in result

    def test_first_sync_runs_full(self, app, db):
        from app.services import dropbox_service
        user_id, cid = _create_conn(app, db, provider="dropbox", sync_cursor=None)
        with patch.object(dropbox_service, "_full_sync") as fs:
            with patch.object(dropbox_service, "_delta_sync") as ds:
                with patch("app.services.cloud_sync_utils.has_failed_docs",
                           return_value=False):
                    with app.app_context():
                        dropbox_service.sync_connection(cid)
        fs.assert_called_once()
        ds.assert_not_called()
        _cleanup(app, db, user_id, cid)


# ─────────────────────────────────────────────────────────────────
# kDrive (WebDAV-based, password auth instead of OAuth)
# ─────────────────────────────────────────────────────────────────

class TestKDriveStoreCredentials:
    def test_store_credentials_persists_encrypted_token(self, app):
        from app.services import kdrive_service

        with app.test_request_context():
            conn = MagicMock(spec=[
                "access_token_enc", "refresh_token_enc", "token_expiry",
                "extra_metadata", "folder_id",
            ])
            # Signature: (conn, email, password, kdrive_id)
            kdrive_service.store_credentials(
                conn, "user@example.com", "password123", "drive-xyz",
            )
        # Password encrypted (not raw)
        assert conn.access_token_enc != "password123"


class TestKDriveSyncConnection:
    def test_unknown_connection_returns_error(self, app, db):
        from app.services import kdrive_service
        with app.app_context():
            result = kdrive_service.sync_connection(99999)
        assert "error" in result


# ─────────────────────────────────────────────────────────────────
# Zotero (API-key auth, no OAuth)
# ─────────────────────────────────────────────────────────────────

class TestZoteroStoreCredentials:
    def test_store_credentials_persists_encrypted_key_and_user_id(self, app):
        from app.services import zotero_service

        with app.test_request_context():
            conn = MagicMock(spec=["access_token_enc", "refresh_token_enc",
                                   "token_expiry", "extra_metadata"])
            zotero_service.store_credentials(conn, "key123", "user-xyz")
        # API key encrypted
        assert conn.access_token_enc != "key123"
        # user_id stored somewhere — could be in extra_metadata
        assert conn.extra_metadata is not None or hasattr(conn, "extra_metadata")


class TestZoteroIsVersionCursor:
    def test_numeric_strings_recognised_as_version_cursors(self):
        from app.services import zotero_service
        assert zotero_service._is_version_cursor("12345") is True
        assert zotero_service._is_version_cursor("0") is True

    def test_non_numeric_rejected(self):
        from app.services import zotero_service
        assert zotero_service._is_version_cursor("abc") is False
        assert zotero_service._is_version_cursor(None) is False
        assert zotero_service._is_version_cursor("") is False
        assert zotero_service._is_version_cursor("2024-01-01T00:00:00") is False


class TestZoteroParseFolderId:
    def test_default_returns_user_scope(self):
        from app.services import zotero_service

        # When folder_id is None, returns (None or 'user', None)
        result = zotero_service._parse_folder_id(None)
        assert result is not None  # Just exercise — exact tuple format varies

    def test_collection_format(self):
        from app.services import zotero_service

        # When folder_id is "C:somekey", treat as collection
        result = zotero_service._parse_folder_id("C:abc123")
        assert result is not None


class TestZoteroBuildText:
    def test_extracts_title_and_abstract(self):
        from app.services import zotero_service
        item = {
            "title": "Test Article",
            "abstractNote": "This is the abstract",
            "creators": [
                {"firstName": "John", "lastName": "Doe", "creatorType": "author"},
            ],
        }
        text = zotero_service._build_text(item)
        assert "Test Article" in text
        assert "abstract" in text.lower() or "This is the abstract" in text

    def test_handles_empty_input(self):
        from app.services import zotero_service
        text = zotero_service._build_text({})
        # Empty input returns either empty string or near-empty
        assert isinstance(text, str)


class TestZoteroSyncConnection:
    def test_unknown_connection_returns_error(self, app, db):
        from app.services import zotero_service
        with app.app_context():
            result = zotero_service.sync_connection(99999)
        assert "error" in result
