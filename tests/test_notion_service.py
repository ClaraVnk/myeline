"""Coverage for app/services/notion_service.py — Notion Integration API.

HTTP boundary at requests.{get,post} mocked.
"""
from unittest.mock import MagicMock, patch


# ─── credential helpers ──────────────────────────────────────────────

class TestCredentials:
    def test_store_and_retrieve(self, app):
        from app.services.notion_service import _get_token, store_credentials
        with app.test_request_context():
            conn = MagicMock(spec=[
                "access_token_enc", "refresh_token_enc",
                "token_expiry", "sync_cursor",
            ])
            store_credentials(conn, "secret_xyz")
            assert _get_token(conn) == "secret_xyz"
            assert conn.refresh_token_enc is None
            assert conn.token_expiry is None
            assert conn.sync_cursor is None

    def test_headers_include_version_and_bearer(self):
        from app.services.notion_service import _headers
        h = _headers("secret_xyz")
        assert h["Authorization"] == "Bearer secret_xyz"
        assert "Notion-Version" in h
        assert h["Content-Type"] == "application/json"


# ─── check_credentials ───────────────────────────────────────────────

class TestCheckCredentials:
    def test_true_on_200(self):
        from app.services.notion_service import check_credentials
        resp = MagicMock(status_code=200)
        with patch("app.services.notion_service.requests.get",
                   return_value=resp):
            assert check_credentials("token") is True

    def test_false_on_non_200(self):
        from app.services.notion_service import check_credentials
        resp = MagicMock(status_code=401)
        with patch("app.services.notion_service.requests.get",
                   return_value=resp):
            assert check_credentials("token") is False

    def test_false_on_exception(self):
        from app.services.notion_service import check_credentials
        with patch("app.services.notion_service.requests.get",
                   side_effect=Exception("net")):
            assert check_credentials("token") is False


# ─── _extract_rich_text + _page_title ───────────────────────────────

class TestRichTextExtraction:
    def test_concatenates_plain_text(self):
        from app.services.notion_service import _extract_rich_text
        result = _extract_rich_text([
            {"plain_text": "Hello "},
            {"plain_text": "world"},
        ])
        assert result == "Hello world"

    def test_handles_missing_keys(self):
        from app.services.notion_service import _extract_rich_text
        assert _extract_rich_text([{}]) == ""


class TestPageTitle:
    def test_extracts_title_from_properties(self):
        from app.services.notion_service import _page_title
        page = {
            "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "My page"}]},
            }
        }
        assert _page_title(page) == "My page"

    def test_falls_back_to_untitled(self):
        from app.services.notion_service import _page_title
        assert _page_title({"properties": {}}) == "Untitled"


# ─── _extract_blocks ──────────────────────────────────────────────────

class TestExtractBlocks:
    def test_returns_text_lines(self):
        from app.services.notion_service import _extract_blocks
        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "results": [
                {"id": "b1", "type": "paragraph",
                 "paragraph": {"rich_text": [{"plain_text": "hello"}]}},
                {"id": "b2", "type": "heading_1",
                 "heading_1": {"rich_text": [{"plain_text": "Title"}]}},
            ],
            "has_more": False,
        }
        with patch("app.services.notion_service.requests.get",
                   return_value=resp):
            lines = _extract_blocks("tok", "page-id")
        assert "hello" in lines
        assert "Title" in lines

    def test_stops_at_max_depth(self):
        from app.services.notion_service import _extract_blocks

        # depth > 6 returns []
        assert _extract_blocks("tok", "page-id", depth=7) == []

    def test_breaks_on_http_error(self):
        from app.services.notion_service import _extract_blocks
        resp = MagicMock(status_code=500)
        with patch("app.services.notion_service.requests.get",
                   return_value=resp):
            assert _extract_blocks("tok", "page-id") == []


# ─── _page_to_text ───────────────────────────────────────────────────

class TestPageToText:
    def test_combines_title_and_blocks(self):
        from app.services import notion_service
        with patch.object(notion_service, "_extract_blocks",
                          return_value=["body line 1", "body line 2"]):
            title, text = notion_service._page_to_text(
                "tok",
                {
                    "id": "P1",
                    "properties": {"Name": {"type": "title",
                                            "title": [{"plain_text": "My"}]}},
                },
            )
        assert title == "My"
        assert "body line 1" in text


# ─── sync_connection ─────────────────────────────────────────────────

class TestSyncConnection:
    def test_returns_error_when_connection_missing(self, app):
        from app.services.notion_service import sync_connection
        with app.app_context():
            with patch("app.ext.db.session.get", return_value=None):
                result = sync_connection(999_999)
        assert "error" in result

    def test_paginates_and_indexes_pages(self, app, db):
        import uuid

        from app.models.cloud_connection import CloudConnection
        from app.models.user import User
        from app.services import notion_service
        with app.app_context():
            u = User(
                email=f"nt-{uuid.uuid4().hex[:6]}@example.com",
                is_confirmed=True, is_active=True,
                abonnement_type="premium_pro",
            )
            u.set_password("Nt1234!")
            db.session.add(u)
            db.session.flush()
            c = CloudConnection(
                user_id=u.id, provider="notion",
                access_token_enc="x", refresh_token_enc=None,
            )
            db.session.add(c)
            db.session.commit()
            cid = c.id

            # Patch _get_token via store_credentials would be cleaner but mock works.
            page_resp = MagicMock()
            page_resp.json.return_value = {
                "results": [
                    {"id": "P1", "last_edited_time": "2026-01-01T00:00:00Z",
                     "properties": {"Name": {"type": "title",
                                              "title": [{"plain_text": "T"}]}}},
                ],
                "has_more": False,
            }
            page_resp.raise_for_status = MagicMock()

            with patch.object(notion_service, "_get_token", return_value="t"), \
                 patch("app.services.notion_service.requests.post",
                       return_value=page_resp), \
                 patch.object(notion_service, "_page_to_text",
                              return_value=("Title", "Content body...")), \
                 patch("app.services.rag_service.index_cloud_document",
                       return_value=2):
                result = notion_service.sync_connection(cid)
        assert "indexed" in result
        assert result["indexed"] >= 1
