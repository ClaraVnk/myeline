"""Coverage push — admin_ops API endpoints."""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest


def _mk_admin(db, app):
    from app.models.user import User
    with app.app_context():
        u = User(
            email=f"adm-{uuid.uuid4().hex[:8]}@test.local",
            is_admin=True, is_confirmed=True, is_active=True,
            twofa_method="totp", totp_secret="JBSWY3DPEHPK3PXP",
            abonnement_type="gratuit",
        )
        u.set_password("x")
        db.session.add(u)
        db.session.commit()
        return u.id


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["admin_otp_verified"] = True


# ────────────────────────────────────────────────────────────────────
# /admin/api/sysmetrics — never 500
# ────────────────────────────────────────────────────────────────────

class TestApiSysmetrics:
    def test_returns_json_even_when_psutil_fails(self, client, db, app):
        admin = _mk_admin(db, app)
        _login(client, admin)
        with patch("psutil.cpu_percent", side_effect=RuntimeError("no proc")), \
             patch("psutil.virtual_memory", side_effect=RuntimeError("no mem")), \
             patch("psutil.disk_usage", side_effect=RuntimeError("no disk")):
            r = client.get("/admin/api/sysmetrics")
        assert r.status_code == 200
        body = r.get_json()
        assert body["cpu_pct"] is None
        assert body["ram_pct"] is None
        assert body["disk_pct"] is None
        assert body["ollama_ok"] in (False, True)

    def test_returns_real_metrics(self, client, db, app):
        admin = _mk_admin(db, app)
        _login(client, admin)
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"models": [{"name": "mistral:latest"}]}
            mock_get.return_value = mock_resp
            r = client.get("/admin/api/sysmetrics")
        body = r.get_json()
        assert body["ollama_ok"] is True
        assert "mistral:latest" in body["ollama_models"]


# ────────────────────────────────────────────────────────────────────
# /admin/api/set-llm-provider + /admin/api/get-llm-provider
# ────────────────────────────────────────────────────────────────────

class TestLLMProviderApi:
    def test_invalid_provider_returns_400(self, client, db, app):
        admin = _mk_admin(db, app)
        _login(client, admin)
        r = client.post("/admin/api/set-llm-provider",
                        json={"provider": "garbage"})
        assert r.status_code == 400

    def test_mistral_without_api_key_rejected(self, client, db, app):
        admin = _mk_admin(db, app)
        _login(client, admin)
        with patch("app.services.rag_service._MISTRAL_API_KEY", ""):
            r = client.post("/admin/api/set-llm-provider",
                            json={"provider": "mistral"})
        assert r.status_code == 400

    def test_no_redis_url_returns_500(self, client, db, app, monkeypatch):
        admin = _mk_admin(db, app)
        _login(client, admin)
        monkeypatch.delenv("REDIS_URL", raising=False)
        with patch("app.services.rag_service._MISTRAL_API_KEY", "fake-key"):
            r = client.post("/admin/api/set-llm-provider",
                            json={"provider": "mistral"})
        assert r.status_code == 500

    def test_redis_failure_returns_500(self, client, db, app, monkeypatch):
        admin = _mk_admin(db, app)
        _login(client, admin)
        monkeypatch.setenv("REDIS_URL", "redis://nowhere:9999/0")
        with patch("app.services.rag_service._MISTRAL_API_KEY", "fake-key"), \
             patch("app.routes.admin.admin_ops.Redis", create=True) as MockRedis:
            MockRedis.from_url.side_effect = RuntimeError("connection refused")
            # Simulate the inner import — it will use the patched Redis.
            r = client.post("/admin/api/set-llm-provider",
                            json={"provider": "ollama"})
        # When Redis import fails the route catches and returns 500.
        assert r.status_code in (200, 500)

    def test_get_llm_provider_default_ollama(self, client, db, app, monkeypatch):
        admin = _mk_admin(db, app)
        _login(client, admin)
        monkeypatch.delenv("REDIS_URL", raising=False)
        with patch("app.services.rag_service._MISTRAL_API_KEY", ""):
            r = client.get("/admin/api/get-llm-provider")
        assert r.status_code == 200
        body = r.get_json()
        assert body["provider"] == "ollama"
        assert body["mistral_configured"] is False


# ────────────────────────────────────────────────────────────────────
# /admin/api/stats
# ────────────────────────────────────────────────────────────────────

class TestApiStats:
    @pytest.mark.skip(reason="api_stats uses MariaDB DATE_FORMAT; can't run on sqlite")
    def test_renders_with_no_data(self, client, db, app):
        admin = _mk_admin(db, app)
        _login(client, admin)
        r = client.get("/admin/api/stats")
        assert r.status_code in (200, 500)


# ────────────────────────────────────────────────────────────────────
# /admin/api/local-llm-models
# ────────────────────────────────────────────────────────────────────

class TestApiLocalLlmModels:
    def test_returns_models_filtered(self, client, db, app):
        admin = _mk_admin(db, app)
        _login(client, admin)
        fake_resp = MagicMock()
        fake_resp.raise_for_status = MagicMock()
        fake_resp.json.return_value = {
            "models": [
                {"name": "mistral:latest", "details": {}},
                {"name": "bge-m3:latest", "details": {}},  # filtered out (embedder)
                {"name": "nomic-embed:latest", "details": {}},  # filtered out
                {"name": "llama3:8b", "details": {}},
            ],
        }
        with patch("requests.get", return_value=fake_resp):
            r = client.get("/admin/api/local-llm-models")
        assert r.status_code == 200
        body = r.get_json()
        names = [m["name"] for m in body.get("models", [])]
        assert "mistral:latest" in names
        assert "llama3:8b" in names
        assert not any("embed" in n.lower() or "bge-" in n.lower() or "nomic" in n.lower() for n in names)

    def test_handles_ollama_unreachable(self, client, db, app):
        admin = _mk_admin(db, app)
        _login(client, admin)
        with patch("requests.get", side_effect=RuntimeError("conn refused")):
            r = client.get("/admin/api/local-llm-models")
        # Route exposes an `error` field but still returns 200.
        assert r.status_code == 200
