"""Coverage for app/services/rag_service.py — pure-function and mocked paths.

The full RAG pipeline needs ChromaDB + Ollama + Mistral, none of which we
spin up for unit tests. This file targets the functions that are pure
(string transforms, scoring, parsers) plus the HTTP-call paths via mocks.
"""
import json
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────

class TestStripHtml:
    def test_strips_tags(self):
        from app.services.rag_service import _strip_html
        assert _strip_html("<p>hello <b>world</b></p>") == "hello world"

    def test_handles_empty(self):
        from app.services.rag_service import _strip_html
        assert _strip_html("") == ""

    def test_collapses_whitespace_separator(self):
        from app.services.rag_service import _strip_html
        out = _strip_html("<p>a</p><p>b</p>")
        assert "a" in out and "b" in out


class TestMaxTokens:
    def test_short_factual_query_gets_1024(self):
        from app.services.rag_service import _max_tokens
        assert _max_tokens("What is Python?") == 1024

    def test_long_query_gets_2048(self):
        from app.services.rag_service import _max_tokens
        long_q = " ".join(f"word{i}" for i in range(20))
        assert _max_tokens(long_q) == 2048

    def test_synthesis_keyword_french_triggers_2048(self):
        from app.services.rag_service import _max_tokens
        assert _max_tokens("résume ce texte") == 2048
        assert _max_tokens("compare A et B") == 2048
        assert _max_tokens("explique le concept") == 2048

    def test_synthesis_keyword_english_triggers_2048(self):
        from app.services.rag_service import _max_tokens
        assert _max_tokens("summarize this") == 2048
        assert _max_tokens("explain that") == 2048


class TestExtractKeywords:
    def test_filters_stopwords(self):
        from app.services.rag_service import _extract_keywords
        kw = _extract_keywords("the cat sat on the mat")
        kw_lower = [k.lower() for k in kw]
        # "the", "on" are stopwords (English variants in _STOPWORDS)
        assert "the" not in kw_lower

    def test_filters_short_tokens(self):
        from app.services.rag_service import _extract_keywords
        kw = _extract_keywords("a b c hello world")
        # 1-2 char tokens excluded by regex \b[a-zA-ZÀ-ÿ]{3,}\b
        assert "a" not in kw and "b" not in kw

    def test_dedupes(self):
        from app.services.rag_service import _extract_keywords
        kw = _extract_keywords("python python python langage")
        unigrams = [k for k in kw if " " not in k]
        # python should appear once at most
        assert unigrams.count("python") == 1

    def test_includes_bigrams(self):
        from app.services.rag_service import _extract_keywords
        kw = _extract_keywords("machine learning algorithm")
        assert any(" " in k for k in kw), "Expected at least one bigram"


class TestTrigramSet:
    def test_short_text_uses_padding(self):
        from app.services.rag_service import _trigram_set
        assert isinstance(_trigram_set("abc"), set)
        # Even short strings produce some trigrams
        assert len(_trigram_set("abc")) >= 1

    def test_text_sim_identical_high(self):
        from app.services.rag_service import _text_sim
        assert _text_sim("hello world", "hello world") > 0.95

    def test_text_sim_disjoint_low(self):
        from app.services.rag_service import _text_sim

        # Two strings with very little char overlap
        assert _text_sim("xxxxxxx", "yyyyyyy") < 0.2


class TestShouldUseHyde:
    def test_short_query_skips_hyde(self):
        from app.services.rag_service import _should_use_hyde
        assert _should_use_hyde("Python") is False

    def test_long_query_uses_hyde(self):
        from app.services.rag_service import _should_use_hyde
        long_q = (
            "What are the architectural trade-offs between event-driven "
            "microservices and a modular monolith for a SaaS product?"
        )
        assert _should_use_hyde(long_q) is True


class TestNomicPrefix:
    def test_no_prefix_for_bge(self, monkeypatch):
        import app.services.rag_service as rag
        monkeypatch.setattr(rag, "_EMBED_MODEL", "bge-m3")
        assert rag._nomic_prefix("text", "search_query") == "text"

    def test_prefix_for_nomic_model(self, monkeypatch):
        import app.services.rag_service as rag
        monkeypatch.setattr(rag, "_EMBED_MODEL", "nomic-embed-text")
        assert rag._nomic_prefix("text", "search_query") == "search_query: text"


class TestHnswMeta:
    def test_returns_required_keys(self):
        from app.services.rag_service import _hnsw_meta
        m = _hnsw_meta()
        for k in ("hnsw:space", "embed_model", "hnsw:M", "hnsw:construction_ef", "hnsw:search_ef"):
            assert k in m

    def test_extra_overrides_base(self):
        from app.services.rag_service import _hnsw_meta
        m = _hnsw_meta(extra={"hnsw:M": 99})
        assert m["hnsw:M"] == 99


class TestUseMistralApi:
    def test_true_when_key_set(self, monkeypatch):
        import app.services.rag_service as rag
        monkeypatch.setattr(rag, "_MISTRAL_API_KEY", "fake-key")
        assert rag._use_mistral_api() is True

    def test_false_when_key_empty(self, monkeypatch):
        import app.services.rag_service as rag
        monkeypatch.setattr(rag, "_MISTRAL_API_KEY", "")
        assert rag._use_mistral_api() is False


# ─────────────────────────────────────────────────────────────────
# Gemini response parser
# ─────────────────────────────────────────────────────────────────

class TestParseGeminiResponse:
    def test_normal_response(self):
        from app.services.rag_service import _parse_gemini_response
        data = {
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": "hello"}, {"text": " world"}]},
            }]
        }
        assert _parse_gemini_response(data) == "hello world"

    def test_max_tokens_keeps_text(self):
        from app.services.rag_service import _parse_gemini_response
        data = {
            "candidates": [{
                "finishReason": "MAX_TOKENS",
                "content": {"parts": [{"text": "truncated"}]},
            }]
        }
        assert _parse_gemini_response(data) == "truncated"

    def test_safety_filter_raises(self):
        from app.services.rag_service import _parse_gemini_response
        data = {"candidates": [{"finishReason": "SAFETY"}]}
        with pytest.raises(ValueError, match="safety"):
            _parse_gemini_response(data)

    def test_recitation_raises(self):
        from app.services.rag_service import _parse_gemini_response
        data = {"candidates": [{"finishReason": "RECITATION"}]}
        with pytest.raises(ValueError, match="recitation"):
            _parse_gemini_response(data)

    def test_no_candidates_raises_with_block_reason(self):
        from app.services.rag_service import _parse_gemini_response
        data = {"promptFeedback": {"blockReason": "OTHER"}}
        with pytest.raises(ValueError, match="OTHER"):
            _parse_gemini_response(data)

    def test_empty_text_raises(self):
        from app.services.rag_service import _parse_gemini_response
        data = {"candidates": [{"finishReason": "STOP", "content": {"parts": []}}]}
        with pytest.raises(ValueError):
            _parse_gemini_response(data)


# ─────────────────────────────────────────────────────────────────
# HTTP retry helpers
# ─────────────────────────────────────────────────────────────────

class TestMistralPostRetry:
    def test_returns_immediately_on_2xx(self):
        from app.services.rag_service import _mistral_post
        ok = MagicMock(status_code=200)
        with patch("app.services.rag_service._HTTP.post", return_value=ok) as p:
            resp = _mistral_post({}, "key", timeout=10)
        assert resp is ok
        assert p.call_count == 1

    def test_retries_on_429_then_succeeds(self):
        from app.services.rag_service import _mistral_post
        rate_limited = MagicMock(status_code=429)
        ok = MagicMock(status_code=200)
        with patch("app.services.rag_service._HTTP.post", side_effect=[rate_limited, ok]):
            with patch("app.services.rag_service.time.sleep"):
                resp = _mistral_post({}, "key", timeout=10, max_retries=3)
        assert resp is ok

    def test_raises_after_all_retries_fail(self):
        from app.services.rag_service import _mistral_post, _MistralRateLimitError
        rate_limited = MagicMock(status_code=429)
        with patch("app.services.rag_service._HTTP.post", return_value=rate_limited):
            with patch("app.services.rag_service.time.sleep"):
                with pytest.raises(_MistralRateLimitError):
                    _mistral_post({}, "key", timeout=10, max_retries=2)


class TestGeminiPostRetry:
    def test_returns_immediately_on_2xx(self):
        from app.services.rag_service import _gemini_post
        ok = MagicMock(status_code=200)
        with patch("app.services.rag_service._HTTP.post", return_value=ok) as p:
            resp = _gemini_post("u", {}, {}, timeout=10)
        assert resp is ok
        assert p.call_count == 1

    def test_retries_on_429_then_succeeds(self):
        from app.services.rag_service import _gemini_post
        rate_limited = MagicMock(status_code=429)
        ok = MagicMock(status_code=200)
        with patch("app.services.rag_service._HTTP.post", side_effect=[rate_limited, ok]):
            with patch("app.services.rag_service.time.sleep"):
                resp = _gemini_post("u", {}, {}, timeout=10, max_retries=3)
        assert resp is ok

    def test_raises_after_all_retries(self):
        from app.services.rag_service import _gemini_post, _GeminiRateLimitError
        rate_limited = MagicMock(status_code=429)
        with patch("app.services.rag_service._HTTP.post", return_value=rate_limited):
            with patch("app.services.rag_service.time.sleep"):
                with pytest.raises(_GeminiRateLimitError):
                    _gemini_post("u", {}, {}, timeout=10, max_retries=2)


# ─────────────────────────────────────────────────────────────────
# _llm_generate routing
# ─────────────────────────────────────────────────────────────────

class TestLlmGenerate:
    def test_routes_to_mistral_when_key_set(self, monkeypatch):
        import app.services.rag_service as rag
        monkeypatch.setattr(rag, "_MISTRAL_API_KEY", "fake-key")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "from-mistral"}}]
        }
        with patch("app.services.rag_service._mistral_post", return_value=mock_resp) as mp:
            out = rag._llm_generate("hello", max_tokens=10)
        assert mp.called
        assert out == "from-mistral"

    def test_falls_back_to_ollama_when_no_mistral(self, monkeypatch):
        import app.services.rag_service as rag
        monkeypatch.setattr(rag, "_MISTRAL_API_KEY", "")
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"response": "from-ollama"}
        with patch("app.services.rag_service._HTTP.post", return_value=mock_resp):
            out = rag._llm_generate("hello", max_tokens=10)
        assert out == "from-ollama"


# ─────────────────────────────────────────────────────────────────
# _embed_query and _embed
# ─────────────────────────────────────────────────────────────────

class TestEmbedQuery:
    def test_calls_ollama_embed_endpoint(self, monkeypatch):
        from app.services import rag_service
        from app.services.rag_service import _embed_query

        # Force Ollama dispatch — EMBED_PROVIDER=mistral would route to API.
        monkeypatch.setattr(rag_service, "_use_mistral_embed", lambda: False)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"embeddings": [[0.1, 0.2, 0.3]]}
        with patch("app.services.rag_service._HTTP.post", return_value=mock_resp) as p:
            out = _embed_query("test query")
        assert out == [0.1, 0.2, 0.3]
        # POST URL contains /api/embed
        called_url = p.call_args.args[0] if p.call_args.args else p.call_args.kwargs.get("url", "")
        assert "/api/embed" in called_url


class TestEmbed:
    def test_empty_input_returns_empty(self):
        from app.services.rag_service import _embed
        assert _embed([]) == []

    def test_batches_large_input(self, monkeypatch):
        from app.services import rag_service
        from app.services.rag_service import _embed
        monkeypatch.setattr(rag_service, "_use_mistral_embed", lambda: False)

        # 100 chunks > _EMBED_BATCH_SIZE (32) → multiple POST calls
        chunks = [f"chunk {i}" for i in range(100)]
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"embeddings": [[0.1] * 5] * 32}
        with patch("app.services.rag_service._HTTP.post", return_value=mock_resp) as p:
            _embed(chunks)
        # ceil(100/32) = 4 batches
        assert p.call_count == 4


# ─────────────────────────────────────────────────────────────────
# Per-org chat router (_chat_mistral, _chat_openai, _chat_gemini, _chat_claude)
# ─────────────────────────────────────────────────────────────────

class TestChatProviders:
    def test_chat_mistral_returns_content(self):
        from app.services.rag_service import _chat_mistral
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "réponse de Mistral"}}]
        }
        with patch("app.services.rag_service._mistral_post", return_value=mock_resp):
            out = _chat_mistral("question", "key", "mistral-medium-latest", 100)
        assert out == "réponse de Mistral"

    def test_chat_openai_returns_content(self):
        from app.services import rag_service
        mock_resp = MagicMock(status_code=200)  # status checked for retry
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "openai answer"}}]
        }
        # OpenAI now goes through the pooled rag_service._HTTP session.
        with patch.object(rag_service._HTTP, "post", return_value=mock_resp):
            out = rag_service._chat_openai("question", "sk-key", "gpt-4o", 100)
        assert out == "openai answer"

    def test_chat_claude_returns_content(self):
        from app.services import rag_service
        mock_resp = MagicMock(status_code=200)  # status checked for retry
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "content": [{"text": "claude answer"}]
        }
        with patch.object(rag_service._HTTP, "post", return_value=mock_resp):
            out = rag_service._chat_claude("question", "sk-ant-key", "claude-haiku", 100)
        assert out == "claude answer"

    def test_chat_gemini_returns_content(self):
        from app.services.rag_service import _chat_gemini
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": "gemini answer"}]},
            }]
        }
        with patch("app.services.rag_service._gemini_post", return_value=mock_resp):
            out = _chat_gemini("question", "key", "gemini-pro", 100)
        assert out == "gemini answer"


# ─────────────────────────────────────────────────────────────────
# _generate_chunk_contexts (parallel batches)
# ─────────────────────────────────────────────────────────────────

class TestGenerateChunkContexts:
    def test_returns_empty_when_no_mistral(self, monkeypatch):
        import app.services.rag_service as rag
        monkeypatch.setattr(rag, "_MISTRAL_API_KEY", "")
        out = rag._generate_chunk_contexts("doc intro", ["chunk1", "chunk2"])
        assert out == ["", ""]

    def test_returns_empty_when_no_chunks(self, monkeypatch):
        import app.services.rag_service as rag
        monkeypatch.setattr(rag, "_MISTRAL_API_KEY", "key")
        out = rag._generate_chunk_contexts("doc intro", [])
        assert out == []

    def test_returns_empty_when_no_intro(self, monkeypatch):
        import app.services.rag_service as rag
        monkeypatch.setattr(rag, "_MISTRAL_API_KEY", "key")
        out = rag._generate_chunk_contexts("", ["c1", "c2"])
        assert out == ["", ""]

    def test_parses_numbered_response(self, monkeypatch):
        import app.services.rag_service as rag
        monkeypatch.setattr(rag, "_MISTRAL_API_KEY", "key")
        with patch.object(
            rag, "_llm_generate",
            return_value="[1] context for chunk 1\n[2] context for chunk 2",
        ):
            out = rag._generate_chunk_contexts("intro", ["chunk1", "chunk2"])
        assert out[0] == "context for chunk 1"
        assert out[1] == "context for chunk 2"

    def test_falls_back_to_empty_on_llm_exception(self, monkeypatch):
        import app.services.rag_service as rag
        monkeypatch.setattr(rag, "_MISTRAL_API_KEY", "key")
        with patch.object(rag, "_llm_generate", side_effect=Exception("boom")):
            out = rag._generate_chunk_contexts("intro", ["c1"])
        assert out == [""]


# ─────────────────────────────────────────────────────────────────
# _hyde_embed — query expansion via hypothetical document
# ─────────────────────────────────────────────────────────────────

class TestHydeEmbed:
    def test_falls_back_to_query_embedding_on_llm_failure(self, monkeypatch):
        import app.services.rag_service as rag
        with patch.object(rag, "_llm_generate", side_effect=Exception("boom")):
            with patch.object(rag, "_embed_query", return_value=[0.5, 0.5]):
                out = rag._hyde_embed("query")
        assert out == [0.5, 0.5]

    def test_uses_generated_doc_when_llm_succeeds(self, monkeypatch):
        import app.services.rag_service as rag
        with patch.object(rag, "_llm_generate", return_value="hypothetical answer"):
            with patch.object(rag, "_embed_query", return_value=[0.9, 0.1]) as eq:
                rag._hyde_embed("query")
        # _embed_query is called with the LLM-generated doc, not the raw query
        assert eq.called
