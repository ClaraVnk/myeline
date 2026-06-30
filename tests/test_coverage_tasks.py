"""Coverage push — app/tasks.py provider-routing + helpers."""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest


# ── sync_cloud_connection_task — one branch per provider ─────────────────────

class TestSyncCloudProviderRouting:
    def _mk_conn(self, db, app, provider):
        from app.models.cloud_connection import CloudConnection
        from app.models.user import User
        with app.app_context():
            u = User(
                email=f"u-{uuid.uuid4().hex[:8]}@test.local",
                is_confirmed=True, is_active=True,
                abonnement_type="premium_pro",
            )
            u.set_password("x")
            db.session.add(u)
            db.session.flush()
            conn = CloudConnection(
                user_id=u.id, provider=provider, access_token_enc="enc",
            )
            db.session.add(conn)
            db.session.commit()
            return conn.id

    @pytest.mark.parametrize("provider,svc_module", [
        ("gdrive", "app.services.gdrive_service"),
        ("onedrive", "app.services.onedrive_service"),
        ("dropbox", "app.services.dropbox_service"),
        ("kdrive", "app.services.kdrive_service"),
        ("zotero", "app.services.zotero_service"),
        ("s3", "app.services.s3_service"),
    ])
    def test_provider_routes_to_correct_service(self, app, db, provider, svc_module):
        from app import tasks
        cid = self._mk_conn(db, app, provider)
        with patch.object(tasks, "_make_app", return_value=app), \
             patch(f"{svc_module}.sync_connection",
                   return_value={"indexed": 7, "errors": 0}) as mock_sync:
            result = tasks.sync_cloud_connection_task(cid)
        assert result == {"indexed": 7, "errors": 0}
        mock_sync.assert_called_once_with(cid)

    def test_unknown_provider_returns_error(self, app, db):
        from app import tasks
        cid = self._mk_conn(db, app, "garbage")
        with patch.object(tasks, "_make_app", return_value=app):
            result = tasks.sync_cloud_connection_task(cid)
        assert result == {"indexed": 0, "errors": 1}

    def test_missing_connection_returns_error(self, app, db):
        from app import tasks
        with patch.object(tasks, "_make_app", return_value=app):
            result = tasks.sync_cloud_connection_task(999_999)
        assert result == {"indexed": 0, "errors": 1}

    def test_service_exception_is_swallowed(self, app, db):
        from app import tasks
        cid = self._mk_conn(db, app, "gdrive")
        with patch.object(tasks, "_make_app", return_value=app), \
             patch("app.services.gdrive_service.sync_connection",
                   side_effect=RuntimeError("api down")):
            result = tasks.sync_cloud_connection_task(cid)
        assert result == {"indexed": 0, "errors": 1}


# ── scrape_source_task ───────────────────────────────────────────────────────

class TestScrapeSourceTask:
    def test_returns_count_on_success(self, app):
        from app import tasks
        with patch.object(tasks, "_make_app", return_value=app), \
             patch("app.services.user_scraper_service.scrape_source",
                   return_value=(5, None)):
            result = tasks.scrape_source_task(1)
        assert result == {"new_articles": 5, "error": None}

    def test_returns_error_when_service_raises(self, app):
        from app import tasks
        with patch.object(tasks, "_make_app", return_value=app), \
             patch("app.services.user_scraper_service.scrape_source",
                   side_effect=RuntimeError("boom")):
            result = tasks.scrape_source_task(1)
        assert result == {"new_articles": 0, "error": True}


# ── _load_conversation_history ──────────────────────────────────────────────

class TestLoadConversationHistory:
    def test_no_conversation_id(self, app):
        from app.tasks import _load_conversation_history
        history, conv = _load_conversation_history(1, None)
        assert history == []
        assert conv is None

    def test_unknown_conversation_returns_empty(self, app):
        from app.tasks import _load_conversation_history
        with app.app_context():
            history, conv = _load_conversation_history(1, 999_999)
        assert history == []
        assert conv is None


# ── _log_query_history pruning ──────────────────────────────────────────────

class TestLogQueryHistoryPrune:
    def test_pruning_kicks_in_above_200(self, app, db):
        """201st row triggers a delete so the table stays at 200."""
        from datetime import datetime, timedelta, timezone

        from app.models.query_history import QueryHistory
        from app.models.user import User
        from app.tasks import _log_query_history

        with app.app_context():
            u = User(
                email=f"qh-{uuid.uuid4().hex[:8]}@test.local",
                is_confirmed=True, is_active=True,
                abonnement_type="gratuit",
            )
            u.set_password("x")
            db.session.add(u)
            db.session.commit()
            uid = u.id
            base = datetime.now(timezone.utc) - timedelta(days=30)
            for i in range(201):
                db.session.add(QueryHistory(
                    user_id=uid, query=f"q{i}", scope="shared",
                    sources_count=0,
                    executed_at=base + timedelta(seconds=i),
                ))
            db.session.commit()

            _log_query_history(uid, "new query", "shared", {})

            count = db.session.query(QueryHistory).filter_by(user_id=uid).count()
            # After insert + prune we should be at 200 (the cap).
            assert count == 200


# ── broadcast_task — additional branches ────────────────────────────────────

class TestBroadcastTaskExtra:
    def test_broadcast_with_empty_recipient_list_is_silent(self, app):
        from app.tasks import broadcast_task
        with patch("app.tasks._make_app", return_value=app):
            # No recipients = no email sends, no exception
            broadcast_task([], "subject", "<p>body</p>", "http://localhost/")

    def test_broadcast_swallows_outer_exception(self, app):
        """The outer try/except logs but never raises (queue safety)."""
        from app.tasks import broadcast_task
        with patch("app.tasks._make_app", return_value=app), \
             patch("app.models.user.User.query") as mock_q:
            mock_q.filter.side_effect = RuntimeError("db dead")
            # Should not raise.
            broadcast_task([1], "subject", "<p>body</p>", "http://localhost/")
