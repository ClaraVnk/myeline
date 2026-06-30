"""Coverage for app/tasks.py — RQ background jobs."""
import uuid
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────
# sync_cloud_connection_task — provider routing
# ─────────────────────────────────────────────────────────────────

class TestSyncCloudConnectionTask:
    def test_returns_error_dict_when_connection_missing(self, app):
        from app.tasks import sync_cloud_connection_task

        with patch("app.tasks._make_app", return_value=app):
            result = sync_cloud_connection_task(connection_id=999_999)
        assert result == {"indexed": 0, "errors": 1}

    def test_routes_to_gdrive(self, app, db):
        from app.models.cloud_connection import CloudConnection
        from app.models.user import User
        from app.tasks import sync_cloud_connection_task

        with app.app_context():
            u = User(email=f"sync-{uuid.uuid4().hex[:6]}@example.com",
                    is_confirmed=True, is_active=True)
            u.set_password("Pass1234!")
            db.session.add(u)
            db.session.flush()
            user_id = u.id

            c = CloudConnection(
                user_id=user_id, provider="gdrive", folder_id=None,
                access_token_enc="x", refresh_token_enc="x",
            )
            db.session.add(c)
            db.session.commit()
            conn_id = c.id

        with patch("app.tasks._make_app", return_value=app):
            with patch("app.services.gdrive_service.sync_connection",
                       return_value={"indexed": 5, "errors": 0}) as svc:
                result = sync_cloud_connection_task(conn_id)

        assert svc.called
        assert result["indexed"] == 5

        with app.app_context():
            c = db.session.get(CloudConnection, conn_id)
            if c:
                db.session.delete(c)
            usr = db.session.get(User, user_id)
            if usr:
                db.session.delete(usr)
            db.session.commit()

    def test_unknown_provider_returns_error(self, app, db):
        from app.models.cloud_connection import CloudConnection
        from app.models.user import User
        from app.tasks import sync_cloud_connection_task

        with app.app_context():
            u = User(email=f"unknown-{uuid.uuid4().hex[:6]}@example.com",
                    is_confirmed=True, is_active=True)
            u.set_password("Pass1234!")
            db.session.add(u)
            db.session.flush()
            user_id = u.id

            c = CloudConnection(
                user_id=user_id, provider="weird-cloud", folder_id=None,
                access_token_enc="x", refresh_token_enc="x",
            )
            db.session.add(c)
            db.session.commit()
            conn_id = c.id

        with patch("app.tasks._make_app", return_value=app):
            result = sync_cloud_connection_task(conn_id)

        assert result == {"indexed": 0, "errors": 1}

        with app.app_context():
            c = db.session.get(CloudConnection, conn_id)
            if c:
                db.session.delete(c)
            usr = db.session.get(User, user_id)
            if usr:
                db.session.delete(usr)
            db.session.commit()

    def test_swallows_exception_and_returns_error_dict(self, app, db):
        from app.models.cloud_connection import CloudConnection
        from app.models.user import User
        from app.tasks import sync_cloud_connection_task

        with app.app_context():
            u = User(email=f"err-{uuid.uuid4().hex[:6]}@example.com",
                    is_confirmed=True, is_active=True)
            u.set_password("Pass1234!")
            db.session.add(u)
            db.session.flush()
            user_id = u.id
            c = CloudConnection(
                user_id=user_id, provider="dropbox", folder_id=None,
                access_token_enc="x", refresh_token_enc="x",
            )
            db.session.add(c)
            db.session.commit()
            conn_id = c.id

        with patch("app.tasks._make_app", return_value=app):
            with patch("app.services.dropbox_service.sync_connection",
                       side_effect=RuntimeError("boom")):
                result = sync_cloud_connection_task(conn_id)
        assert result == {"indexed": 0, "errors": 1}

        with app.app_context():
            c = db.session.get(CloudConnection, conn_id)
            if c:
                db.session.delete(c)
            usr = db.session.get(User, user_id)
            if usr:
                db.session.delete(usr)
            db.session.commit()


# ─────────────────────────────────────────────────────────────────
# scrape_source_task
# ─────────────────────────────────────────────────────────────────

class TestScrapeSourceTask:
    def test_success_returns_new_articles(self, app):
        from app.tasks import scrape_source_task
        with patch("app.tasks._make_app", return_value=app):
            with patch("app.services.user_scraper_service.scrape_source",
                       return_value=(7, None)) as scr:
                result = scrape_source_task(scraper_id=1)
        assert scr.called
        assert result == {"new_articles": 7, "error": None}

    def test_exception_returns_error_dict(self, app):
        from app.tasks import scrape_source_task
        with patch("app.tasks._make_app", return_value=app):
            with patch("app.services.user_scraper_service.scrape_source",
                       side_effect=Exception("network")):
                result = scrape_source_task(scraper_id=1)
        assert result == {"new_articles": 0, "error": True}


# ─────────────────────────────────────────────────────────────────
# rag_query_task
# ─────────────────────────────────────────────────────────────────

class TestRagQueryTask:
    def test_calls_search_and_returns_result(self, app):
        from app.tasks import rag_query_task
        fake_result = {"answer": "answer here", "sources": [{"url": "x"}]}
        with patch("app.tasks._make_app", return_value=app):
            with patch("app.services.rag_service.search", return_value=fake_result):
                with patch("app.tasks._log_query_history"):  # avoid DB write
                    result = rag_query_task(user_id=1, query="hello", tag_filter=None, scope="shared")
        assert result == fake_result

    def test_swallows_exception_and_returns_friendly_error(self, app):
        from app.tasks import rag_query_task
        with patch("app.tasks._make_app", return_value=app):
            with patch("app.services.rag_service.search", side_effect=RuntimeError("LLM down")):
                result = rag_query_task(user_id=1, query="hello", tag_filter=None, scope="shared")
        assert "error" in result
        assert "réessayer" in result["error"].lower() or "internal" in result["error"].lower()


# ─────────────────────────────────────────────────────────────────
# _log_query_history
# ─────────────────────────────────────────────────────────────────

class TestLogQueryHistory:
    def test_creates_query_history_row(self, app, db):
        from app.models.query_history import QueryHistory
        from app.models.user import User
        from app.tasks import _log_query_history

        with app.app_context():
            u = User(email=f"qh-{uuid.uuid4().hex[:6]}@example.com",
                    is_confirmed=True, is_active=True)
            u.set_password("Pass1234!")
            db.session.add(u)
            db.session.commit()

            _log_query_history(
                user_id=u.id, query="my question", scope="shared",
                result={"answer": "the answer", "sources": [{"url": "a"}, {"url": "b"}]},
            )

            row = db.session.query(QueryHistory).filter(QueryHistory.user_id == u.id).first()
            assert row is not None
            assert row.query == "my question"
            assert row.scope == "shared"
            assert row.sources_count == 2

            db.session.query(QueryHistory).filter(QueryHistory.user_id == u.id).delete()
            db.session.delete(u)
            db.session.commit()

    def test_truncates_long_query(self, app, db):
        from app.models.query_history import QueryHistory
        from app.models.user import User
        from app.tasks import _log_query_history

        with app.app_context():
            u = User(email=f"qh-trunc-{uuid.uuid4().hex[:6]}@example.com",
                    is_confirmed=True, is_active=True)
            u.set_password("Pass1234!")
            db.session.add(u)
            db.session.commit()

            long_query = "x" * 1000
            _log_query_history(
                user_id=u.id, query=long_query, scope="shared",
                result={"answer": "y" * 2000, "sources": []},
            )
            row = db.session.query(QueryHistory).filter(QueryHistory.user_id == u.id).first()
            assert len(row.query) == 400
            # answer_excerpt capped at 500
            assert row.answer_excerpt is not None
            assert len(row.answer_excerpt) == 500

            db.session.query(QueryHistory).filter(QueryHistory.user_id == u.id).delete()
            db.session.delete(u)
            db.session.commit()

    def test_does_not_raise_on_db_error(self, app):
        from app.tasks import _log_query_history

        with app.app_context():
            with patch("app.ext.db.session.add", side_effect=Exception("db down")):
                # Must NOT raise
                _log_query_history(
                    user_id=999, query="q", scope="shared",
                    result={"answer": "a", "sources": []},
                )


# ─────────────────────────────────────────────────────────────────
# broadcast_task
# ─────────────────────────────────────────────────────────────────

class TestBroadcastTask:
    def test_sends_to_each_recipient(self, app, db):
        from app.models.user import User
        from app.tasks import broadcast_task

        with app.app_context():
            users = []
            for _ in range(3):
                u = User(email=f"bc-{uuid.uuid4().hex[:6]}@example.com",
                        firstname="B", lastname="C",
                        is_confirmed=True, is_active=True)
                u.set_password("Pass1234!")
                db.session.add(u)
                users.append(u)
            db.session.commit()
            ids = [u.id for u in users]

        with patch("app.tasks._make_app", return_value=app):
            with patch("app.utils.mailer.send_email_via_brevo_api") as send:
                broadcast_task(
                    recipient_ids=ids,
                    subject="Hello",
                    html_content="<p>body</p>",
                    url_root="https://myeline.io",
                )
        # 3 emails dispatched
        assert send.call_count == 3

        with app.app_context():
            for uid in ids:
                u = db.session.get(User, uid)
                if u:
                    db.session.delete(u)
            db.session.commit()

    def test_handles_empty_recipient_list(self, app):
        from app.tasks import broadcast_task

        with patch("app.tasks._make_app", return_value=app):
            with patch("app.utils.mailer.send_email_via_brevo_api") as send:
                # Must not raise on empty list
                broadcast_task(
                    recipient_ids=[], subject="x", html_content="x",
                    url_root="https://myeline.io",
                )
        assert send.call_count == 0

    def test_swallows_per_recipient_exception(self, app, db):
        from app.models.user import User
        from app.tasks import broadcast_task

        with app.app_context():
            u = User(email=f"failer-{uuid.uuid4().hex[:6]}@example.com",
                    firstname="F", lastname="L",
                    is_confirmed=True, is_active=True)
            u.set_password("Pass1234!")
            db.session.add(u)
            db.session.commit()
            uid = u.id

        with patch("app.tasks._make_app", return_value=app):
            with patch("app.utils.mailer.send_email_via_brevo_api",
                       side_effect=RuntimeError("Brevo down")):
                # Must complete without raising
                broadcast_task(
                    recipient_ids=[uid], subject="x", html_content="x",
                    url_root="https://myeline.io",
                )

        with app.app_context():
            u = db.session.get(User, uid)
            if u:
                db.session.delete(u)
                db.session.commit()


# ─────────────────────────────────────────────────────────────────
# _make_app
# ─────────────────────────────────────────────────────────────────

class TestMakeApp:
    def test_returns_a_flask_app(self):
        from app.tasks import _make_app
        app = _make_app()
        assert hasattr(app, "config")
        assert hasattr(app, "test_request_context")
