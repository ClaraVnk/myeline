"""Coverage push — services edge-cases.

Targets uncovered branches in:
- app/services/rag_service.py: cache hits/misses, fallbacks
- app/services/user_scraper_service.py: error paths
- app/services/cloud_sync_utils.py: small helpers
- app/services/quotas.py: edge cases
- app/utils/safe_http.py: missing branches if any
"""
from unittest.mock import MagicMock, patch

import pytest


# ── rag_service: cache, embedding cache, query variants cache ─────────────────

class TestEmbedCache:
    def test_embed_cache_hit_returns_cached_value(self, monkeypatch):
        """When Redis has the key, _embed_query returns it without HTTP."""
        import json

        from app.services import rag_service
        cached = [0.1, 0.2, 0.3]
        fake_redis = MagicMock()
        fake_redis.get.return_value = json.dumps(cached).encode()
        with patch.object(rag_service, "_get_cache", return_value=fake_redis), \
             patch.object(rag_service._HTTP, "post") as mock_post:
            result = rag_service._embed_query("hello world")
        assert result == cached
        # No HTTP call needed — the cache hit short-circuits.
        mock_post.assert_not_called()

    def test_embed_cache_miss_falls_through_to_ollama(self, monkeypatch):
        """When Redis misses, _embed_query hits Ollama and writes the result."""
        from app.services import rag_service

        # Force Ollama dispatch path (EMBED_PROVIDER=mistral in env would route to API).
        monkeypatch.setattr(rag_service, "_use_mistral_embed", lambda: False)
        fake_redis = MagicMock()
        fake_redis.get.return_value = None
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"embeddings": [[0.5, 0.6]]}
        fake_resp.raise_for_status = MagicMock()
        with patch.object(rag_service, "_get_cache", return_value=fake_redis), \
             patch.object(rag_service._HTTP, "post", return_value=fake_resp) as mock_post:
            result = rag_service._embed_query("query xyz")
        assert result == [0.5, 0.6]
        mock_post.assert_called_once()
        fake_redis.set.assert_called_once()

    def test_embed_no_cache_configured_skips_lookup(self, monkeypatch):
        """When _get_cache returns None, the function fetches directly without cache I/O."""
        from app.services import rag_service
        monkeypatch.setattr(rag_service, "_use_mistral_embed", lambda: False)
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"embeddings": [[1.0]]}
        fake_resp.raise_for_status = MagicMock()
        with patch.object(rag_service, "_get_cache", return_value=None), \
             patch.object(rag_service._HTTP, "post", return_value=fake_resp):
            result = rag_service._embed_query("nope")
        assert result == [1.0]


class TestVariantCache:
    def test_generate_variants_cache_hit(self):
        """The second call with the same query reads from the Redis cache."""
        import json

        from app.services import rag_service
        cached_variants = ["original", "variant a", "variant b"]
        fake_redis = MagicMock()
        fake_redis.get.return_value = json.dumps(cached_variants).encode()
        with patch.object(rag_service, "_get_cache", return_value=fake_redis), \
             patch.object(rag_service, "_llm_generate") as mock_llm:
            result = rag_service._generate_query_variants("test query")
        assert result == cached_variants
        mock_llm.assert_not_called()

    def test_generate_variants_falls_back_to_query_on_llm_error(self):
        """If the LLM call raises, we return [query] alone."""
        from app.services import rag_service
        fake_redis = MagicMock()
        fake_redis.get.return_value = None
        with patch.object(rag_service, "_get_cache", return_value=fake_redis), \
             patch.object(rag_service, "_llm_generate", side_effect=RuntimeError("boom")):
            result = rag_service._generate_query_variants("only-me")
        assert result == ["only-me"]


# ── rag_service: HyDE cache + parallel paths ─────────────────────────────────

class TestHydeEmbed:
    def test_hyde_cache_hit_skips_llm_and_embed(self):
        """Cache hit on HyDE returns the precomputed combined embedding."""
        import json

        from app.services import rag_service
        cached_combined = [0.7, 0.8]
        fake_redis = MagicMock()
        fake_redis.get.return_value = json.dumps(cached_combined).encode()
        with patch.object(rag_service, "_get_cache", return_value=fake_redis), \
             patch.object(rag_service, "_llm_generate") as mock_llm, \
             patch.object(rag_service, "_embed_query") as mock_embed:
            result = rag_service._hyde_embed("question?")
        assert result == cached_combined
        mock_llm.assert_not_called()
        mock_embed.assert_not_called()

    def test_hyde_falls_back_to_plain_embed_when_llm_fails(self):
        """When LLM hypothetical generation throws, plain query embed is returned."""
        from app.services import rag_service
        fake_redis = MagicMock()
        fake_redis.get.return_value = None
        with patch.object(rag_service, "_get_cache", return_value=fake_redis), \
             patch.object(rag_service, "_llm_generate", side_effect=RuntimeError("LLM down")), \
             patch.object(rag_service, "_embed_query", return_value=[0.1, 0.2]):
            result = rag_service._hyde_embed("any q")
        assert result == [0.1, 0.2]


# ── rag_service: synthesis flag bypass ───────────────────────────────────────

class TestSearchSynthesisFlag:
    def test_synthesis_false_skips_chat(self):
        """search(synthesis=False) returns sources without calling _chat."""
        from app.services import rag_service
        fake_col = MagicMock()
        fake_col.name = "test"
        fake_col.metadata = {"hnsw:search_ef": 200}
        fake_col.count.return_value = 0
        fake_col.query.return_value = {
            "ids": [[]], "documents": [[]],
            "metadatas": [[]], "distances": [[]],
        }
        with patch.object(rag_service, "_get_cache", return_value=None), \
             patch.object(rag_service, "_embed_query", return_value=[0.1]), \
             patch.object(rag_service, "_get_collection", return_value=fake_col), \
             patch.object(rag_service, "_chat") as mock_chat:
            out = rag_service.search("hello", scope="shared", synthesis=False)
        # No chat call regardless of whether the empty-result placeholder is used.
        mock_chat.assert_not_called()
        # Sources list must be empty since the collection is empty.
        assert out.get("sources", []) == []


# ── safe_http: tiny missing-branch coverage ─────────────────────────────────

class TestSafeHttpEdges:
    def test_module_imports_cleanly(self):
        """The module can be imported and exposes a public fetch function."""
        from app.utils import safe_http

        # At minimum a "get" or fetch helper must exist
        assert any(
            hasattr(safe_http, n)
            for n in ("safe_get", "fetch", "http_get", "get")
        )


# ── cloud_sync_utils: helpers (not yet exercised) ─────────────────────────────

class TestCloudSyncUtils:
    def test_start_sync_progress_resets_counters(self, app):
        """start_sync_progress sets total_files and resets processed."""
        from app.ext import db
        from app.services.cloud_sync_utils import start_sync_progress
        with app.app_context():
            conn = MagicMock()
            conn.id = 99
            with patch.object(db.session, "commit") as mock_commit, \
                 patch.object(db.session, "execute"):
                start_sync_progress(conn, 10)
            assert conn.sync_total_files == 10
            assert conn.sync_processed_files == 0
            mock_commit.assert_called()

    def test_clear_sync_progress_nullifies_fields(self, app):
        """clear_sync_progress sets the counter columns to None."""
        from app.ext import db
        from app.services.cloud_sync_utils import clear_sync_progress
        with app.app_context():
            conn = MagicMock()
            with patch.object(db.session, "commit"):
                clear_sync_progress(conn)
            assert conn.sync_total_files is None
            assert conn.sync_processed_files is None


# ── user_scraper_service: error paths ────────────────────────────────────────

class TestScraperServiceEdges:
    def test_scrape_inactive_scraper_returns_zero(self, app, db):
        """scrape_source returns (0, error) for an inactive scraper."""
        from app.models.user import User
        from app.models.user_scraper import UserScraper
        from app.services.user_scraper_service import scrape_source
        with app.app_context():
            u = User(email="scrape-edge@test.local",
                     is_confirmed=True, is_active=True,
                     abonnement_type="premium_pro")
            u.set_password("x")
            db.session.add(u)
            db.session.flush()
            sc = UserScraper(
                user_id=u.id, name="dead", url="http://example.com",
                scraper_type="rss", is_active=False,
            )
            db.session.add(sc)
            db.session.commit()
            sid = sc.id
            count, err = scrape_source(sid)
        assert count == 0
        assert err and ("inactif" in err.lower() or "introuvable" in err.lower())

    def test_scrape_unknown_id_returns_error(self, app):
        """scrape_source(unknown id) returns the same friendly error."""
        from app.services.user_scraper_service import scrape_source
        with app.app_context():
            count, err = scrape_source(999_999)
        assert count == 0
        assert err is not None


# ── deployment_mode helpers ──────────────────────────────────────────────────

class TestDeploymentModeHelpers:
    @pytest.mark.parametrize("mode,expected_classic,expected_sovereign,expected_hybrid", [
        ("classic", True, False, False),
        ("sovereign", False, True, False),
        ("hybrid", False, False, True),
    ])
    def test_predicates(self, monkeypatch, mode, expected_classic,
                        expected_sovereign, expected_hybrid):
        """is_classic / is_sovereign / is_hybrid map correctly to DEPLOYMENT_MODE."""
        from app.utils import deployment_mode as dm
        monkeypatch.setenv("DEPLOYMENT_MODE", mode)
        # Clear lru_cache if applicable
        for attr in ("get_deployment_mode", "is_classic", "is_sovereign", "is_hybrid"):
            f = getattr(dm, attr, None)
            if hasattr(f, "cache_clear"):
                f.cache_clear()
        assert dm.is_classic() is expected_classic
        assert dm.is_sovereign() is expected_sovereign
        assert dm.is_hybrid() is expected_hybrid


# ── tasks.py: rag_query_task error path ──────────────────────────────────────

class TestRagQueryTaskErrors:
    def test_search_exception_returns_error_dict(self, app):
        """When search() raises, the task returns {'error': ...}."""
        from app import tasks
        with app.app_context():
            with patch.object(tasks, "_make_app", return_value=app), \
                 patch("app.services.rag_service.search",
                       side_effect=RuntimeError("oops")):
                result = tasks.rag_query_task(
                    user_id=1, query="q", tag_filter=None,
                    scope="shared",
                )
        assert "error" in result
