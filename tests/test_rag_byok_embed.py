"""Coverage for the per-user BYOK embedding routing in rag_service.

These tests mock the HTTP layer — no live calls to Mistral/OpenAI/Voyage/
Cohere. The point is to verify dispatch (right URL + right headers + right
payload shape) and the safety harness (dim-mismatch guard, cache key
namespace, resolver fallback).
"""
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────
# _resolve_embed_for_user — the core lookup
# ─────────────────────────────────────────────────────────────────

class TestResolveEmbedForUser:
    def setup_method(self):
        # LRU cache survives across tests — reset before each.
        from app.services.rag_service import invalidate_user_embed_resolution
        invalidate_user_embed_resolution(0)

    def test_returns_none_for_unset_user(self):
        from app.services.rag_service import _resolve_embed_for_user
        assert _resolve_embed_for_user(None) is None

    def test_returns_none_when_user_has_no_byok(self, monkeypatch):
        from app.services import rag_service
        user = MagicMock()
        user.embed_provider = None
        user.embed_api_key_enc = None
        with patch.object(
            rag_service, "_resolve_embed_for_user_cached", return_value=None,
        ):
            assert rag_service._resolve_embed_for_user(42) is None

    def test_returns_config_when_user_has_byok(self):
        from app.services import rag_service
        from app.services.rag_service import _resolve_embed_for_user
        with patch.object(
            rag_service, "_resolve_embed_for_user_cached",
            return_value=("voyage", "voyage-3", "voyage-key", 128, 1024),
        ):
            out = _resolve_embed_for_user(42)
        assert out == {
            "provider": "voyage",
            "model": "voyage-3",
            "api_key": "voyage-key",
            "batch_size": 128,
            "dim": 1024,
        }

    def test_raises_when_decrypt_failed_sentinel(self):
        """Audit round 4 — A3-H1. A decrypt failure must surface as
        BYOKDecryptError rather than fall back to the default provider."""
        import pytest

        from app.services import rag_service
        from app.services.rag_service import _DECRYPT_FAILED, BYOKDecryptError, _resolve_embed_for_user
        with patch.object(
            rag_service, "_resolve_embed_for_user_cached",
            return_value=("voyage", _DECRYPT_FAILED, None, None, None),
        ):
            with pytest.raises(BYOKDecryptError) as exc:
                _resolve_embed_for_user(99)
        assert exc.value.user_id == 99
        assert exc.value.provider == "voyage"


class TestDecryptFailurePropagation:
    """End-to-end: a decrypt failure on the user BYOK key must NOT silently
    fall back to the default provider in _embed / _embed_query."""

    def test_embed_raises_on_decrypt_failure(self):
        import pytest

        from app.services import rag_service
        from app.services.rag_service import BYOKDecryptError, _embed
        with patch.object(
            rag_service, "_resolve_embed_for_user",
            side_effect=BYOKDecryptError(5, "openai"),
        ):
            with pytest.raises(BYOKDecryptError):
                _embed(["test"], user_id=5)

    def test_embed_query_raises_on_decrypt_failure(self):
        import pytest

        from app.services import rag_service
        from app.services.rag_service import BYOKDecryptError, _embed_query
        with patch.object(
            rag_service, "_resolve_embed_for_user",
            side_effect=BYOKDecryptError(5, "openai"),
        ):
            with pytest.raises(BYOKDecryptError):
                _embed_query("test", user_id=5)


# ─────────────────────────────────────────────────────────────────
# Per-provider HTTP backends
# ─────────────────────────────────────────────────────────────────

class TestEmbedMistralByok:
    def test_calls_mistral_url_with_byok_key(self):
        from app.services import rag_service
        from app.services.rag_service import _embed_mistral_batch_byok
        mock_resp = MagicMock(status_code=200)
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [
                {"index": 0, "embedding": [0.1] * 1024},
                {"index": 1, "embedding": [0.2] * 1024},
            ],
        }
        with patch.object(rag_service._HTTP, "post", return_value=mock_resp) as p:
            out = _embed_mistral_batch_byok(["a", "b"], "byok-key", "mistral-embed")
        assert out == [[0.1] * 1024, [0.2] * 1024]
        url = p.call_args.kwargs.get("url") or p.call_args.args[0]
        assert "api.mistral.ai" in url
        headers = p.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer byok-key"
        body = p.call_args.kwargs["json"]
        assert body == {"model": "mistral-embed", "input": ["a", "b"]}


class TestEmbedOpenai:
    def test_calls_openai_url(self):
        from app.services import rag_service
        from app.services.rag_service import _embed_openai_batch
        mock_resp = MagicMock(status_code=200)
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [{"index": 0, "embedding": [0.7] * 1536}],
        }
        with patch.object(rag_service._HTTP, "post", return_value=mock_resp) as p:
            out = _embed_openai_batch(
                ["hello"], "sk-test", "text-embedding-3-small",
            )
        assert out == [[0.7] * 1536]
        url = p.call_args.kwargs.get("url") or p.call_args.args[0]
        assert "openai.com" in url
        assert p.call_args.kwargs["headers"]["Authorization"] == "Bearer sk-test"

    def test_preserves_input_order_even_when_response_is_shuffled(self):
        from app.services import rag_service
        from app.services.rag_service import _embed_openai_batch
        mock_resp = MagicMock(status_code=200)
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [
                {"index": 1, "embedding": [9.0]},
                {"index": 0, "embedding": [1.0]},
            ],
        }
        with patch.object(rag_service._HTTP, "post", return_value=mock_resp):
            out = _embed_openai_batch(
                ["first", "second"], "sk", "text-embedding-3-small",
            )
        assert out == [[1.0], [9.0]]


class TestEmbedVoyage:
    def test_sends_input_type_document(self):
        from app.services import rag_service
        from app.services.rag_service import _embed_voyage_batch
        mock_resp = MagicMock(status_code=200)
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [{"index": 0, "embedding": [0.3] * 1024}],
        }
        with patch.object(rag_service._HTTP, "post", return_value=mock_resp) as p:
            _embed_voyage_batch(["x"], "voyage-key", "voyage-3")
        body = p.call_args.kwargs["json"]
        assert body["model"] == "voyage-3"
        assert body["input_type"] == "document"
        assert body["input"] == ["x"]
        url = p.call_args.kwargs.get("url") or p.call_args.args[0]
        assert "voyageai" in url


class TestEmbedCohere:
    def test_parses_new_response_shape(self):
        from app.services import rag_service
        from app.services.rag_service import _embed_cohere_batch
        mock_resp = MagicMock(status_code=200)
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "embeddings": {"float": [[0.5, 0.6]]},
        }
        with patch.object(rag_service._HTTP, "post", return_value=mock_resp) as p:
            out = _embed_cohere_batch(
                ["x"], "cohere-key", "embed-multilingual-v3.0",
            )
        assert out == [[0.5, 0.6]]
        body = p.call_args.kwargs["json"]
        assert body["model"] == "embed-multilingual-v3.0"
        assert body["input_type"] == "search_document"
        assert body["texts"] == ["x"]

    def test_parses_legacy_flat_shape(self):
        from app.services import rag_service
        from app.services.rag_service import _embed_cohere_batch
        mock_resp = MagicMock(status_code=200)
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"embeddings": [[0.1, 0.2]]}
        with patch.object(rag_service._HTTP, "post", return_value=mock_resp):
            out = _embed_cohere_batch(
                ["x"], "k", "embed-multilingual-v3.0",
            )
        assert out == [[0.1, 0.2]]


class TestBackoffRetry:
    def test_retries_on_429_then_succeeds(self):
        from app.services import rag_service
        from app.services.rag_service import _embed_openai_batch

        # First call 429, second 200.
        fail = MagicMock(status_code=429)
        ok = MagicMock(status_code=200)
        ok.raise_for_status = MagicMock()
        ok.json.return_value = {"data": [{"index": 0, "embedding": [1.0]}]}
        with patch.object(
            rag_service._HTTP, "post", side_effect=[fail, ok],
        ):
            with patch.object(rag_service.time, "sleep"):
                out = _embed_openai_batch(["x"], "k", "model")
        assert out == [[1.0]]

    def test_gives_up_after_max_retries(self):
        from app.services import rag_service
        from app.services.rag_service import _embed_voyage_batch
        fail = MagicMock(status_code=503)
        with patch.object(rag_service._HTTP, "post", return_value=fail):
            with patch.object(rag_service.time, "sleep"):
                with pytest.raises(rag_service._MistralRateLimitError):
                    _embed_voyage_batch(["x"], "k", "voyage-3")

    def test_wall_time_budget_aborts_retry(self):
        """Audit round 4 — A3-H4. When the cumulative sleep budget would
        be exceeded, the harness must break out instead of sleeping for
        the next exponential delay."""
        from app.services import rag_service
        from app.services.rag_service import _backoff_embed_post
        fail = MagicMock(status_code=429)
        with patch.object(rag_service._HTTP, "post", return_value=fail), \
             patch.object(rag_service.time, "sleep") as sleep, \
             patch.object(rag_service.time, "monotonic", side_effect=[
                 0.0, 0.5, 100.0, 100.0,  # second iteration sees we're past deadline
             ]):
            with pytest.raises(rag_service._MistralRateLimitError):
                _backoff_embed_post(
                    url="http://example/",
                    headers={},
                    payload={},
                    parse=lambda j: [],
                    provider_label="Test",
                    max_retries=5,
                    max_total_seconds=2.0,
                )
        # Slept at most once before bailing.
        assert sleep.call_count <= 1


# ─────────────────────────────────────────────────────────────────
# _embed / _embed_query dispatch when user has BYOK
# ─────────────────────────────────────────────────────────────────

class TestEmbedDispatchByok:
    def test_embed_routes_to_byok_backend_when_user_set(self):
        from app.services import rag_service
        from app.services.rag_service import _embed
        backend = MagicMock(return_value=[[0.42] * 4])
        with patch.object(
            rag_service, "_resolve_embed_for_user",
            return_value={
                "provider": "openai",
                "model": "text-embedding-3-small",
                "api_key": "sk-x",
                "batch_size": 256,
                "dim": 1536,
            },
        ), patch.dict(
            rag_service._BYOK_EMBED_BACKENDS, {"openai": backend},
        ):
            out = _embed(["foo"], doc_title="doc", user_id=7)
        assert out == [[0.42] * 4]
        backend.assert_called_once_with(["doc\nfoo"], "sk-x", "text-embedding-3-small")

    def test_embed_query_routes_to_byok_backend(self):
        from app.services import rag_service
        from app.services.rag_service import _embed_query
        backend = MagicMock(return_value=[[0.9, 0.8]])
        with patch.object(
            rag_service, "_resolve_embed_for_user",
            return_value={
                "provider": "cohere",
                "model": "embed-multilingual-v3.0",
                "api_key": "cohere-k",
                "batch_size": 96,
                "dim": 1024,
            },
        ), patch.dict(
            rag_service._BYOK_EMBED_BACKENDS, {"cohere": backend},
        ), patch.object(rag_service, "_get_cache", return_value=None):
            out = _embed_query("query", user_id=7)
        assert out == [0.9, 0.8]
        backend.assert_called_once_with(["query"], "cohere-k", "embed-multilingual-v3.0")

    def test_embed_falls_back_to_default_when_no_user(self, monkeypatch):
        """user_id=None must keep the legacy path untouched."""
        from app.services import rag_service
        from app.services.rag_service import _embed
        monkeypatch.setattr(rag_service, "_use_mistral_embed", lambda: False)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"embeddings": [[0.1] * 5]}
        with patch.object(
            rag_service, "_resolve_embed_for_user", return_value=None,
        ):
            with patch.object(rag_service._HTTP, "post", return_value=mock_resp):
                out = _embed(["foo"])
        assert out == [[0.1] * 5]


# ─────────────────────────────────────────────────────────────────
# Cache key includes provider
# ─────────────────────────────────────────────────────────────────

class TestEmbedCacheKey:
    def test_default_key_includes_provider(self):
        from app.services import rag_service
        from app.services.rag_service import _embed_cache_key
        with patch.object(rag_service, "_active_embed_provider", return_value="ollama"):
            with patch.object(rag_service, "_active_embed_model", return_value="bge-m3"):
                key = _embed_cache_key("hello")
        assert key.startswith("embed:ollama:bge-m3:")

    def test_byok_user_key_distinct_from_default(self):
        from app.services import rag_service
        from app.services.rag_service import _embed_cache_key

        # Default provider
        with patch.object(rag_service, "_resolve_embed_for_user", return_value=None):
            with patch.object(rag_service, "_use_mistral_embed", return_value=False):
                k_default = _embed_cache_key("query")
        # Same text under BYOK voyage
        with patch.object(
            rag_service, "_resolve_embed_for_user",
            return_value={"provider": "voyage", "model": "voyage-3", "api_key": "k",
                          "batch_size": 128, "dim": 1024},
        ):
            k_byok = _embed_cache_key("query", user_id=5)
        assert k_default != k_byok
        assert ":voyage:" in k_byok
        assert ":voyage-3:" in k_byok
