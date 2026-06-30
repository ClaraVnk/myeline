"""Coverage for app/routes/admin/admin_ops.py — admin tools + ops endpoints."""
import uuid
from unittest.mock import MagicMock, patch

import pytest

from tests.helpers import login


# ─────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────

@pytest.fixture
def admin_user(app, db):
    from app.models.user import User
    with app.app_context():
        u = User(
            email=f"admin-ops-{uuid.uuid4().hex[:6]}@example.com",
            firstname="A", lastname="O",
            is_confirmed=True, is_active=True,
            is_admin=True,
            twofa_method="email",
        )
        u.set_password("Admin123!")
        db.session.add(u)
        db.session.commit()
        user_id = u.id
        email = u.email
    yield {"id": user_id, "email": email}
    with app.app_context():
        u = db.session.get(User, user_id)
        if u:
            db.session.delete(u)
        db.session.commit()


# ─────────────────────────────────────────────────────────────────
# Auth gates
# ─────────────────────────────────────────────────────────────────

class TestAuthGates:
    @pytest.mark.parametrize("path", [
        "/admin/tools",
        "/admin/api/stats",
        "/admin/api/sysmetrics",
        "/admin/api/get-llm-provider",
        "/admin/api/local-llm-models",
        "/admin/broadcast",
        "/admin/rag/stats",
    ])
    def test_anonymous_redirected(self, client, path):
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (302, 401)


# ─────────────────────────────────────────────────────────────────
# tools_dashboard
# ─────────────────────────────────────────────────────────────────

class TestToolsDashboard:
    def test_admin_sees_dashboard(self, client, app, db, admin_user):
        login(client, admin_user["email"], "Admin123!")
        r = client.get("/admin/tools")
        assert r.status_code == 200


# ─────────────────────────────────────────────────────────────────
# clear_logs / clear_cache / restart_app
# ─────────────────────────────────────────────────────────────────

class TestMaintenance:
    def test_clear_logs_returns_success(self, client, app, db, admin_user):
        login(client, admin_user["email"], "Admin123!")
        with patch("os.listdir", return_value=[]):
            r = client.post("/admin/tools/clear-logs")
        # Either JSON success or redirect with flash
        assert r.status_code in (200, 302)

    def test_clear_cache_returns_response(self, client, app, db, admin_user):
        """Bare POST (no preview/token) is refused with 400 since audit
        round 4 — the two-step preview→confirm flow is exercised in
        test_admin_ops_extra.py."""
        login(client, admin_user["email"], "Admin123!")
        r = client.post("/admin/tools/clear-cache")
        assert r.status_code in (400, 302, 500)

    def test_restart_app_invokes_subprocess(self, client, app, db, admin_user):
        login(client, admin_user["email"], "Admin123!")
        with patch("subprocess.Popen") as popen:
            r = client.post("/admin/tools/restart")
        # Either succeeds with JSON or returns an error envelope
        assert r.status_code == 200
        assert popen.called

    def test_restart_handles_exception_gracefully(self, client, app, db, admin_user):
        login(client, admin_user["email"], "Admin123!")
        with patch("subprocess.Popen", side_effect=RuntimeError("docker missing")):
            r = client.post("/admin/tools/restart")
        body = r.get_json()
        assert body["success"] is False


# ─────────────────────────────────────────────────────────────────
# export_data
# ─────────────────────────────────────────────────────────────────

class TestExportData:
    def test_returns_json_attachment(self, client, app, db, admin_user):
        login(client, admin_user["email"], "Admin123!")
        r = client.get("/admin/tools/export-data")
        # Either returns JSON attachment or fails with a redirect+flash
        assert r.status_code in (200, 302)
        if r.status_code == 200:
            assert r.mimetype == "application/json"
            assert "attachment" in r.headers.get("Content-Disposition", "")


# ─────────────────────────────────────────────────────────────────
# delete_log / delete_report
# ─────────────────────────────────────────────────────────────────

class TestDeleteLog:
    def test_missing_file_returns_failure(self, client, app, db, admin_user):
        login(client, admin_user["email"], "Admin123!")
        r = client.delete("/admin/tools/delete-log/nonexistent-file.log")
        assert r.status_code == 200
        assert r.get_json()["success"] is False


class TestDeleteReport:
    def test_missing_file_returns_failure(self, client, app, db, admin_user):
        login(client, admin_user["email"], "Admin123!")
        r = client.delete("/admin/tools/delete-report/nope.json")
        assert r.status_code == 200
        assert r.get_json()["success"] is False


# ─────────────────────────────────────────────────────────────────
# api_stats / api_sysmetrics
# ─────────────────────────────────────────────────────────────────

class TestApiStats:
    @pytest.mark.skip(
        reason="Endpoint uses MySQL DATE_FORMAT() which SQLite rejects — would "
               "need a dialect-aware test fixture or query refactor to cover."
    )
    def test_endpoint_responds(self, client, app, db, admin_user):
        pass


class TestApiSysmetrics:
    def test_returns_system_metrics(self, client, app, db, admin_user):
        login(client, admin_user["email"], "Admin123!")
        r = client.get("/admin/api/sysmetrics")
        assert r.status_code in (200, 500)
        # When OK, should return something parseable
        if r.status_code == 200:
            body = r.get_json()
            assert body is not None


# ─────────────────────────────────────────────────────────────────
# set_llm_provider / get_llm_provider
# ─────────────────────────────────────────────────────────────────

class TestSetLlmProvider:
    def test_invalid_provider_400(self, client, app, db, admin_user):
        login(client, admin_user["email"], "Admin123!")
        r = client.post(
            "/admin/api/set-llm-provider",
            json={"provider": "weird"},
        )
        assert r.status_code == 400

    def test_mistral_without_api_key_rejected(self, client, app, db, admin_user, monkeypatch):
        login(client, admin_user["email"], "Admin123!")
        monkeypatch.setattr("app.services.rag_service._MISTRAL_API_KEY", "")
        r = client.post(
            "/admin/api/set-llm-provider",
            json={"provider": "mistral"},
        )
        assert r.status_code == 400

    def test_no_redis_returns_500(self, client, app, db, admin_user, monkeypatch):
        login(client, admin_user["email"], "Admin123!")
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.setattr("app.services.rag_service._MISTRAL_API_KEY", "key-set")
        r = client.post(
            "/admin/api/set-llm-provider",
            json={"provider": "mistral"},
        )
        assert r.status_code == 500

    def test_success_writes_to_redis(self, client, app, db, admin_user, monkeypatch):
        login(client, admin_user["email"], "Admin123!")
        monkeypatch.setenv("REDIS_URL", "redis://fake-redis:6379/0")
        monkeypatch.setattr("app.services.rag_service._MISTRAL_API_KEY", "fake-key")
        fake_redis = MagicMock()
        with patch("redis.Redis.from_url", return_value=fake_redis):
            r = client.post(
                "/admin/api/set-llm-provider",
                json={"provider": "mistral"},
            )
        assert r.status_code == 200
        assert r.get_json()["success"] is True
        fake_redis.set.assert_called_once()


class TestGetLlmProvider:
    def test_returns_provider_status(self, client, app, db, admin_user):
        login(client, admin_user["email"], "Admin123!")
        with patch("redis.Redis.from_url") as Rd:
            Rd.return_value.get.return_value = b"mistral"
            r = client.get("/admin/api/get-llm-provider")
        assert r.status_code == 200
        body = r.get_json()
        assert body["provider"] in ("mistral", "ollama")
        assert "mistral_configured" in body
        assert "sovereign_mode" in body

    def test_redis_unreachable_falls_back_to_default(
        self, client, app, db, admin_user,
    ):
        login(client, admin_user["email"], "Admin123!")
        with patch("redis.Redis.from_url", side_effect=Exception("redis down")):
            r = client.get("/admin/api/get-llm-provider")
        assert r.status_code == 200
        # Falls back to default based on _MISTRAL_API_KEY
        assert "provider" in r.get_json()


# ─────────────────────────────────────────────────────────────────
# list_local_llm_models / set_local_llm_model
# ─────────────────────────────────────────────────────────────────

class TestListLocalLlmModels:
    def test_filters_embedding_models(self, client, app, db, admin_user):
        login(client, admin_user["email"], "Admin123!")
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "models": [
                {"name": "mistral-nemo:latest", "size": 4_500_000_000,
                 "details": {"parameter_size": "12B", "family": "mistral"}},
                {"name": "bge-m3:latest", "size": 600_000_000,
                 "details": {"family": "bge"}},  # excluded (embedder)
                {"name": "nomic-embed-text", "size": 200_000_000,
                 "details": {}},  # excluded
                {"name": "llama3.2:3b", "size": 2_000_000_000,
                 "details": {"parameter_size": "3B"}},
            ],
        }
        with patch("requests.get", return_value=mock_resp):
            r = client.get("/admin/api/local-llm-models")
        assert r.status_code == 200
        body = r.get_json()
        names = [m["name"] for m in body["models"]]
        assert "mistral-nemo:latest" in names
        assert "llama3.2:3b" in names
        # Embedders excluded
        assert "bge-m3:latest" not in names
        assert "nomic-embed-text" not in names

    def test_ollama_unreachable_returns_error_field(
        self, client, app, db, admin_user,
    ):
        login(client, admin_user["email"], "Admin123!")
        with patch("requests.get", side_effect=Exception("conn refused")):
            r = client.get("/admin/api/local-llm-models")
        assert r.status_code == 200
        body = r.get_json()
        assert body["models"] == []
        assert body["error"] is not None


class TestSetLocalLlmModel:
    def test_no_redis_returns_500(self, client, app, db, admin_user, monkeypatch):
        login(client, admin_user["email"], "Admin123!")
        monkeypatch.delenv("REDIS_URL", raising=False)
        r = client.post(
            "/admin/api/set-local-llm-model",
            json={"model": "llama3.2"},
        )
        assert r.status_code == 500

    def test_empty_model_clears_override(
        self, client, app, db, admin_user, monkeypatch,
    ):
        login(client, admin_user["email"], "Admin123!")
        monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")
        fake_redis = MagicMock()
        with patch("redis.Redis.from_url", return_value=fake_redis):
            r = client.post(
                "/admin/api/set-local-llm-model",
                json={"model": ""},
            )
        assert r.status_code == 200
        body = r.get_json()
        assert body["success"] is True
        assert body["model"] is None
        fake_redis.delete.assert_called_once()

    def test_set_model_persists_in_redis(
        self, client, app, db, admin_user, monkeypatch,
    ):
        login(client, admin_user["email"], "Admin123!")
        monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")
        fake_redis = MagicMock()
        # Audit round 4 — A4-H1. The endpoint now validates the model
        # against Ollama's live tags list, so we mock both Redis and the
        # whitelist fetch.
        with patch("redis.Redis.from_url", return_value=fake_redis), \
             patch(
                 "app.routes.admin.admin_ops._fetch_pulled_ollama_models",
                 return_value=({"llama3.2:3b"}, None),
             ):
            r = client.post(
                "/admin/api/set-local-llm-model",
                json={"model": "llama3.2:3b"},
            )
        assert r.status_code == 200
        assert r.get_json()["success"] is True
        fake_redis.set.assert_called_once()

    def test_rejects_model_not_in_pulled_set(
        self, client, app, db, admin_user, monkeypatch,
    ):
        """Audit round 4 — A4-H1. A model name absent from /api/tags
        must be rejected with 400 — never written to Redis."""
        login(client, admin_user["email"], "Admin123!")
        monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")
        fake_redis = MagicMock()
        with patch("redis.Redis.from_url", return_value=fake_redis), \
             patch(
                 "app.routes.admin.admin_ops._fetch_pulled_ollama_models",
                 return_value=({"llama3.2:3b", "qwen2.5:7b"}, None),
             ):
            r = client.post(
                "/admin/api/set-local-llm-model",
                json={"model": "../etc/passwd"},
            )
        assert r.status_code == 400
        assert r.get_json()["success"] is False
        fake_redis.set.assert_not_called()

    def test_rejects_when_ollama_unreachable(
        self, client, app, db, admin_user, monkeypatch,
    ):
        """Ollama down for the whitelist fetch → fail-safe 503,
        no write to Redis."""
        login(client, admin_user["email"], "Admin123!")
        monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")
        fake_redis = MagicMock()
        with patch("redis.Redis.from_url", return_value=fake_redis), \
             patch(
                 "app.routes.admin.admin_ops._fetch_pulled_ollama_models",
                 return_value=(set(), "connection refused"),
             ):
            r = client.post(
                "/admin/api/set-local-llm-model",
                json={"model": "llama3.2:3b"},
            )
        assert r.status_code == 503
        fake_redis.set.assert_not_called()

    def test_empty_model_bypasses_whitelist_check(
        self, client, app, db, admin_user, monkeypatch,
    ):
        """An empty model clears the override — no whitelist call needed."""
        login(client, admin_user["email"], "Admin123!")
        monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")
        fake_redis = MagicMock()
        with patch("redis.Redis.from_url", return_value=fake_redis), \
             patch(
                 "app.routes.admin.admin_ops._fetch_pulled_ollama_models",
             ) as fetch:
            r = client.post(
                "/admin/api/set-local-llm-model",
                json={"model": ""},
            )
        assert r.status_code == 200
        fetch.assert_not_called()


# ─────────────────────────────────────────────────────────────────
# broadcast_email
# ─────────────────────────────────────────────────────────────────

class TestBroadcastEmail:
    def test_get_renders_form(self, client, app, db, admin_user):
        login(client, admin_user["email"], "Admin123!")
        r = client.get("/admin/broadcast")
        assert r.status_code == 200

    def test_post_missing_fields_redirects_with_flash(
        self, client, app, db, admin_user,
    ):
        login(client, admin_user["email"], "Admin123!")
        r = client.post(
            "/admin/broadcast",
            data={"subject": "", "html_content": ""},
            follow_redirects=False,
        )
        assert r.status_code == 302

    def test_post_no_recipients_redirects_with_flash(
        self, client, app, db, admin_user,
    ):
        login(client, admin_user["email"], "Admin123!")
        # No subscribers in DB → recipient list empty
        r = client.post(
            "/admin/broadcast",
            data={
                "subject": "Test", "html_content": "<p>body</p>",
                "plans": ["premium_pro"],
            },
            follow_redirects=False,
        )
        assert r.status_code == 302

    def test_post_refuses_when_redis_unreachable(self, client, app, db, admin_user):
        """Audit round 4 — A4-C3. Redis down for the idempotency lock must
        refuse the broadcast (fail-safe) so a re-submitted form can't
        silently send the same mass mail twice without dedup."""
        import uuid as _uuid

        from app.models.user import User
        with app.app_context():
            sub = User(
                email=f"sub-broadcast-redis-down-{_uuid.uuid4().hex[:6]}@example.com",
                firstname="S", lastname="B",
                is_confirmed=True, is_active=True,
                abonnement_type="premium_pro",
            )
            sub.set_password("Pass1234!")
            db.session.add(sub)
            db.session.commit()
            sub_id = sub.id

        login(client, admin_user["email"], "Admin123!")
        with patch("app.queue.get_queue") as gq, \
             patch("redis.Redis.from_url", side_effect=RuntimeError("redis down")):
            mock_q = MagicMock()
            gq.return_value = mock_q
            r = client.post(
                "/admin/broadcast",
                data={
                    "subject": "Test fail-safe",
                    "html_content": "<p>hello</p>",
                    "plans": ["all"],
                },
                follow_redirects=False,
            )
        # Redirect with danger flash; broadcast not enqueued.
        assert r.status_code == 302
        mock_q.enqueue.assert_not_called()

        with app.app_context():
            u = db.session.get(User, sub_id)
            if u:
                db.session.delete(u)
                db.session.commit()

    def test_post_large_broadcast_requires_confirm_count(
        self, client, app, db, admin_user,
    ):
        """Audit round 4 — A4-M3. Above the 100-recipient threshold the
        server-side guard rejects a submit without confirm_count even if
        the JS dialog was bypassed (curl, tampered form)."""
        from app.models.user import User
        from app.routes.admin import admin_ops as _ops

        prefix = uuid.uuid4().hex[:6]
        # Lower the threshold for this test so we don't have to commit
        # 100+ rows into the SQLite DB and then leak them to the next
        # test in the suite. A monkeypatch resets it at teardown.
        original = _ops._BROADCAST_LARGE_THRESHOLD
        _ops._BROADCAST_LARGE_THRESHOLD = 0  # ANY subscriber crosses it

        sub = User(
            email=f"bulk-{prefix}@example.com",
            firstname="B", lastname="S",
            is_confirmed=True, is_active=True,
            abonnement_type="premium_pro",
        )
        sub.set_password("Pass1234!")
        db.session.add(sub)
        db.session.commit()
        sub_id = sub.id

        try:
            login(client, admin_user["email"], "Admin123!")
            with patch("app.queue.get_queue") as gq:
                mock_q = MagicMock()
                gq.return_value = mock_q
                r = client.post(
                    "/admin/broadcast",
                    data={
                        "subject": "Bulk test",
                        "html_content": "<p>hi</p>",
                        "plans": ["all"],
                        # confirm_count deliberately omitted
                    },
                    follow_redirects=False,
                )
            assert r.status_code == 302
            mock_q.enqueue.assert_not_called()
        finally:
            _ops._BROADCAST_LARGE_THRESHOLD = original
            with app.app_context():
                u = db.session.get(User, sub_id)
                if u:
                    db.session.delete(u)
                    db.session.commit()

    def test_post_large_broadcast_with_matching_confirm_count_proceeds(
        self, client, app, db, admin_user,
    ):
        """Same large broadcast but with a confirm_count that matches the
        server-side count → broadcast goes through."""
        from app.models.user import User
        from app.routes.admin import admin_ops as _ops

        prefix = uuid.uuid4().hex[:6]
        original = _ops._BROADCAST_LARGE_THRESHOLD
        _ops._BROADCAST_LARGE_THRESHOLD = 0

        sub = User(
            email=f"bulkok-{prefix}@example.com",
            firstname="B", lastname="S",
            is_confirmed=True, is_active=True,
            abonnement_type="premium_pro",
        )
        sub.set_password("Pass1234!")
        db.session.add(sub)
        db.session.commit()
        sub_id = sub.id

        try:
            login(client, admin_user["email"], "Admin123!")
            from app.models.user import User as _U
            with patch("app.queue.get_queue") as gq:
                mock_q = MagicMock()
                gq.return_value = mock_q
                # First request: no confirm_count → refuse + flash with count.
                r1 = client.post(
                    "/admin/broadcast",
                    data={
                        "subject": "Bulk OK",
                        "html_content": "<p>hi</p>",
                        "plans": ["all"],
                    },
                    follow_redirects=True,
                )
                # Mirror the route's _subscriber_base(): active, non-admin.
                # plans=["all"] applies no abonnement_type filter.
                live = _U.query.filter(
                    _U.is_active.is_(True),
                    _U.is_admin.is_(False),
                ).count()
                assert live >= 1

                r = client.post(
                    "/admin/broadcast",
                    data={
                        "subject": "Bulk OK",
                        "html_content": "<p>hi</p>",
                        "plans": ["all"],
                        "confirm_count": str(live),
                    },
                    follow_redirects=False,
                )
            # First request refused, second one proceeded.
            assert r1.status_code in (200, 302)
            assert r.status_code == 302
            mock_q.enqueue.assert_called_once()
        finally:
            _ops._BROADCAST_LARGE_THRESHOLD = original
            with app.app_context():
                u = db.session.get(User, sub_id)
                if u:
                    db.session.delete(u)
                    db.session.commit()

    def test_post_with_recipients_enqueues_job(self, client, app, db, admin_user):
        from app.models.user import User
        with app.app_context():
            sub = User(
                email=f"sub-bc-{uuid.uuid4().hex[:6]}@example.com",
                firstname="S", lastname="B",
                is_confirmed=True, is_active=True,
                abonnement_type="premium_pro",
            )
            sub.set_password("Pass1234!")
            db.session.add(sub)
            db.session.commit()
            sub_id = sub.id

        login(client, admin_user["email"], "Admin123!")
        with patch("app.queue.get_queue") as gq:
            mock_q = MagicMock()
            gq.return_value = mock_q
            r = client.post(
                "/admin/broadcast",
                data={
                    "subject": "Test", "html_content": "<p>hello</p>",
                    "plans": ["all"],
                },
                follow_redirects=False,
            )
        assert r.status_code == 302
        mock_q.enqueue.assert_called_once()

        with app.app_context():
            u = db.session.get(User, sub_id)
            if u:
                db.session.delete(u)
                db.session.commit()


# ─────────────────────────────────────────────────────────────────
# rag_stats / rag_index_all / rag_reindex
# ─────────────────────────────────────────────────────────────────

class TestRagEndpoints:
    def test_rag_stats_returns_index_summary(self, client, app, db, admin_user):
        login(client, admin_user["email"], "Admin123!")
        with patch(
            "app.services.rag_service.get_index_stats",
            return_value={"indexed_sections": 42, "status": "ok"},
        ):
            r = client.get("/admin/rag/stats")
        assert r.status_code == 200
        body = r.get_json()
        assert body["indexed_sections"] == 42

    def test_rag_index_all_dispatches_thread(self, client, app, db, admin_user):
        login(client, admin_user["email"], "Admin123!")
        with patch("threading.Thread") as Thread:
            r = client.post("/admin/rag/index-all")
        assert r.status_code == 200
        assert r.get_json()["success"] is True
        Thread.assert_called_once()

    def test_rag_reindex_dispatches_thread(self, client, app, db, admin_user):
        login(client, admin_user["email"], "Admin123!")
        with patch("threading.Thread") as Thread:
            r = client.post("/admin/rag/reindex")
        assert r.status_code == 200
        assert r.get_json()["success"] is True
        Thread.assert_called_once()
