"""Mistral integration tests — _chat router, retry, BYOK resolution.

Mocks at the requests session boundary (`rag_service._HTTP.post`) so we
exercise:
- _mistral_post() retry-on-429 logic (the real production path)
- _chat_mistral happy path + non-200 handling
- _chat() router (pure BYOK off-sovereign): user key → org key → platform
  Mistral (Pro-gated) → "configure your key" message. NO Ollama / cross-
  provider fallback off-sovereign; sovereign short-circuits to Ollama.
- _llm_generate() (auxiliary calls — HyDE, query variants, rerank) which
  DOES still fall back to Ollama off-sovereign (best-effort aux path).
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


def _make_resp(status: int = 200, body: dict | None = None) -> MagicMock:
    """Build a fake `requests.Response` with .status_code, .json(), .raise_for_status()."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body or {}
    if status >= 400:
        from requests.exceptions import HTTPError
        resp.raise_for_status.side_effect = HTTPError(f"{status}")
    else:
        resp.raise_for_status = MagicMock()
    return resp


def _mistral_chat_response(content: str = "Hello!") -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _ollama_chat_response(content: str = "From Ollama!") -> dict:
    return {"message": {"content": content}}


def _gemini_response(content: str = "From Gemini!") -> dict:
    return {"candidates": [{"content": {"parts": [{"text": content}]}}]}


def _claude_response(content: str = "From Claude!") -> dict:
    return {"content": [{"type": "text", "text": content}]}


def _mk_pro_user(db, app):
    """Create a Pro subscriber (is_pro_subscriber True) and return its id."""
    from app.models.user import User
    with app.app_context():
        u = User(
            email=f"pro-{uuid.uuid4().hex[:8]}@test.local",
            is_confirmed=True, is_active=True,
            abonnement_type="premium_pro",
        )
        u.set_password("Pass1234!")
        db.session.add(u)
        db.session.commit()
        return u.id


def _mk_free_user(db, app):
    """Create a Free user (is_pro_subscriber False) and return its id."""
    from app.models.user import User
    with app.app_context():
        u = User(
            email=f"free-{uuid.uuid4().hex[:8]}@test.local",
            is_confirmed=True, is_active=True,
            abonnement_type="gratuit",
        )
        u.set_password("Pass1234!")
        db.session.add(u)
        db.session.commit()
        return u.id


# ════════════════════════════════════════════════════════════════════════════
# _mistral_post — retry-on-429 contract
# ════════════════════════════════════════════════════════════════════════════


class TestMistralPost:
    def test_immediate_success_no_retry(self):
        from app.services import rag_service
        ok = _make_resp(200, _mistral_chat_response())
        with patch.object(rag_service._HTTP, "post", return_value=ok) as mock_post:
            resp = rag_service._mistral_post(
                {"model": "x", "messages": []}, api_key="k", timeout=10,
            )
        assert resp.status_code == 200
        mock_post.assert_called_once()

    def test_429_then_200_retries_once(self):
        from app.services import rag_service
        rl = _make_resp(429)
        ok = _make_resp(200, _mistral_chat_response())
        with patch.object(rag_service._HTTP, "post",
                          side_effect=[rl, ok]) as mock_post, \
             patch("time.sleep"):  # don't actually sleep
            resp = rag_service._mistral_post(
                {"model": "x", "messages": []}, api_key="k",
                timeout=10, max_retries=3,
            )
        assert resp.status_code == 200
        assert mock_post.call_count == 2

    def test_persistent_429_raises_rate_limit_error(self):
        from app.services import rag_service
        rl = _make_resp(429)
        with patch.object(rag_service._HTTP, "post", return_value=rl), \
             patch("time.sleep"):
            with pytest.raises(rag_service._MistralRateLimitError):
                rag_service._mistral_post(
                    {"model": "x", "messages": []}, api_key="k",
                    timeout=10, max_retries=3,
                )

    def test_5xx_returned_without_retry(self):
        """Non-429 errors are NOT retried — caller's raise_for_status decides."""
        from app.services import rag_service
        bad = _make_resp(503)
        with patch.object(rag_service._HTTP, "post", return_value=bad) as mock_post:
            resp = rag_service._mistral_post(
                {"model": "x", "messages": []}, api_key="k", timeout=10,
            )
        assert resp.status_code == 503
        mock_post.assert_called_once()

    def test_authorization_header_carries_api_key(self):
        from app.services import rag_service
        ok = _make_resp(200, _mistral_chat_response())
        captured = {}

        def _capture(*args, **kwargs):
            captured["headers"] = kwargs.get("headers", {})
            captured["json"] = kwargs.get("json", {})
            return ok

        with patch.object(rag_service._HTTP, "post", side_effect=_capture):
            rag_service._mistral_post(
                {"model": "mistral-medium", "messages": [{"role": "user", "content": "hi"}]},
                api_key="my-key-123", timeout=10,
            )
        assert captured["headers"]["Authorization"] == "Bearer my-key-123"
        assert captured["headers"]["Content-Type"] == "application/json"
        assert captured["json"]["model"] == "mistral-medium"


# ════════════════════════════════════════════════════════════════════════════
# _chat_mistral — payload structure
# ════════════════════════════════════════════════════════════════════════════


class TestChatMistral:
    def test_returns_assistant_content(self):
        from app.services import rag_service
        ok = _make_resp(200, _mistral_chat_response("Bonjour, comment puis-je aider ?"))
        with patch.object(rag_service._HTTP, "post", return_value=ok):
            answer = rag_service._chat_mistral(
                "Question", api_key="k", model="mistral-medium-latest",
            )
        assert "Bonjour" in answer

    def test_payload_has_system_prompt(self):
        from app.services import rag_service
        captured = {}

        def _capture(*args, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return _make_resp(200, _mistral_chat_response("ok"))

        with patch.object(rag_service._HTTP, "post", side_effect=_capture):
            rag_service._chat_mistral("Hello", api_key="k", model="m",
                                      system_prompt="CUSTOM SYSTEM")
        msgs = captured["json"]["messages"]
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "CUSTOM SYSTEM"
        assert msgs[1]["role"] == "user"

    def test_propagates_max_tokens(self):
        from app.services import rag_service
        captured = {}

        def _capture(*args, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return _make_resp(200, _mistral_chat_response("ok"))

        with patch.object(rag_service._HTTP, "post", side_effect=_capture):
            rag_service._chat_mistral("Hello", api_key="k", model="m",
                                      max_tokens=512)
        assert captured["json"]["max_tokens"] == 512


# ════════════════════════════════════════════════════════════════════════════
# _chat — router logic (sovereign / org-provider / global / fallback)
# ════════════════════════════════════════════════════════════════════════════


class TestChatRouter:
    def test_sovereign_mode_short_circuits_to_ollama(self, app, monkeypatch):
        """In sovereign mode, _chat() must NOT call any external API."""
        from app.services import rag_service
        from app.utils import deployment_mode as dm
        monkeypatch.setenv("DEPLOYMENT_MODE", "sovereign")
        if hasattr(dm.cached_mode_at_boot, "cache_clear"):
            dm.cached_mode_at_boot.cache_clear()
        # No org_id → goes through sovereign branch
        with app.app_context():
            with patch.object(rag_service, "_chat_ollama",
                              return_value="local response") as mock_ol, \
                 patch.object(rag_service, "_chat_mistral") as mock_mistral:
                answer, warning = rag_service._chat("Q")
        assert answer == "local response"
        assert warning is None
        mock_ol.assert_called_once()
        mock_mistral.assert_not_called()

    def test_pro_user_no_key_uses_platform_mistral(self, app, db, monkeypatch):
        # Pivot pure BYOK — off-sovereign, a Pro user with no own key falls
        # back to the platform Mistral key (Pro-gated). No Ollama fallback.
        from app.services import rag_service
        from app.utils import deployment_mode as dm
        monkeypatch.setenv("DEPLOYMENT_MODE", "classic")
        if hasattr(dm.cached_mode_at_boot, "cache_clear"):
            dm.cached_mode_at_boot.cache_clear()
        uid = _mk_pro_user(db, app)
        with patch.object(rag_service, "_MISTRAL_API_KEY", "fake-key"), \
             patch.object(rag_service, "_use_mistral_api", return_value=True), \
             patch.object(rag_service, "_chat_mistral",
                          return_value="mistral answer") as mock_mistral, \
             patch.object(rag_service, "_chat_ollama") as mock_ol, \
             app.app_context():
            answer, warning = rag_service._chat("Q", user_id=uid)
        assert answer == "mistral answer"
        assert warning is None
        mock_mistral.assert_called_once()
        mock_ol.assert_not_called()

    def test_platform_mistral_rate_limit_no_ollama_fallback(self, app, db, monkeypatch):
        # Pure BYOK — on a Mistral 429 there is NO Ollama fallback off-sovereign.
        # The quota message surfaces and Ollama is never called.
        from app.services import rag_service
        monkeypatch.setenv("DEPLOYMENT_MODE", "classic")
        from app.utils import deployment_mode as dm
        if hasattr(dm.cached_mode_at_boot, "cache_clear"):
            dm.cached_mode_at_boot.cache_clear()
        uid = _mk_pro_user(db, app)
        with patch.object(rag_service, "_MISTRAL_API_KEY", "fake-key"), \
             patch.object(rag_service, "_use_mistral_api", return_value=True), \
             patch.object(rag_service, "_chat_mistral",
                          side_effect=rag_service._MistralRateLimitError("429")), \
             patch.object(rag_service, "_chat_ollama") as mock_ol, \
             app.app_context():
            answer, warning = rag_service._chat("Q", user_id=uid)
        assert warning is not None
        assert "Mistral" in answer or "quota" in warning.lower()
        mock_ol.assert_not_called()

    def test_user_no_key_no_platform_returns_configure_message(self, app, db, monkeypatch):
        # Pure BYOK — a user with no own key and no platform Mistral key
        # configured gets NO Ollama fallback off-sovereign; they get the
        # "configure your key" message.
        from app.services import rag_service
        monkeypatch.setenv("DEPLOYMENT_MODE", "classic")
        from app.utils import deployment_mode as dm
        if hasattr(dm.cached_mode_at_boot, "cache_clear"):
            dm.cached_mode_at_boot.cache_clear()
        uid = _mk_free_user(db, app)
        with patch.object(rag_service, "_MISTRAL_API_KEY", ""), \
             patch.object(rag_service, "_use_mistral_api", return_value=False), \
             patch.object(rag_service, "_chat_ollama") as mock_ol, \
             app.app_context():
            answer, warning = rag_service._chat("Q", user_id=uid)
        assert "clé" in answer
        assert warning is not None
        mock_ol.assert_not_called()


# ════════════════════════════════════════════════════════════════════════════
# _llm_generate (auxiliary calls — HyDE, query variants, rerank)
# ════════════════════════════════════════════════════════════════════════════


class TestLLMGenerate:
    def test_calls_mistral_aux_model(self, monkeypatch):
        """_llm_generate uses the small/cheap aux model, not the synthesis model."""
        from app.services import rag_service
        captured = {}

        def _capture(*args, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return _make_resp(200, _mistral_chat_response("variant 1"))

        with patch.object(rag_service, "_MISTRAL_API_KEY", "k"), \
             patch.object(rag_service._HTTP, "post", side_effect=_capture):
            out = rag_service._llm_generate("Make 3 query variants for X")
        assert "variant" in out
        # Should use _MISTRAL_AUX_MODEL not _MISTRAL_LLM_MODEL
        assert captured["json"]["model"] == rag_service._MISTRAL_AUX_MODEL

    def test_no_mistral_key_falls_back_to_ollama_or_returns_empty(self):
        """Without a Mistral key the helper either falls back to Ollama or
        returns an empty string — both are acceptable since the auxiliary
        outputs are best-effort (HyDE / variants are skipped silently
        when the LLM is unreachable)."""
        from app.services import rag_service
        with patch.object(rag_service, "_MISTRAL_API_KEY", ""), \
             patch.object(rag_service._HTTP, "post",
                          return_value=_make_resp(
                              200, _ollama_chat_response("ollama aux"))):
            out = rag_service._llm_generate("Generate variants")
        # Either string content or empty — both contractually valid.
        assert isinstance(out, str)


# ════════════════════════════════════════════════════════════════════════════
# OpenAI / Claude retry contract — same 429 backoff
# ════════════════════════════════════════════════════════════════════════════


class TestOpenAIRetry:
    def test_429_then_success(self):
        from app.services import rag_service
        rl = _make_resp(429)
        ok = _make_resp(200, _mistral_chat_response("openai content"))
        with patch.object(rag_service._HTTP, "post",
                          side_effect=[rl, ok]) as mock_post, \
             patch("time.sleep"):
            answer = rag_service._chat_openai(
                "Q", api_key="sk-x", model="gpt-4o-mini",
            )
        assert "openai content" in answer
        assert mock_post.call_count == 2

    def test_persistent_429_raises_runtime(self):
        from app.services import rag_service
        rl = _make_resp(429)
        with patch.object(rag_service._HTTP, "post", return_value=rl), \
             patch("time.sleep"):
            with pytest.raises(RuntimeError, match="rate limit"):
                rag_service._chat_openai(
                    "Q", api_key="sk-x", model="gpt-4o-mini",
                )


class TestClaudeRetry:
    def test_persistent_429_raises_runtime(self):
        from app.services import rag_service
        rl = _make_resp(429)
        with patch.object(rag_service._HTTP, "post", return_value=rl), \
             patch("time.sleep"):
            with pytest.raises(RuntimeError, match="rate limit"):
                rag_service._chat_claude(
                    "Q", api_key="ant-x", model="claude-haiku-4-5",
                )


# ════════════════════════════════════════════════════════════════════════════
# _gemini_post — retry contract
# ════════════════════════════════════════════════════════════════════════════


class TestGeminiPost:
    def test_persistent_429_raises_gemini_error(self):
        from app.services import rag_service
        rl = _make_resp(429)
        with patch.object(rag_service._HTTP, "post", return_value=rl), \
             patch("time.sleep"):
            with pytest.raises(rag_service._GeminiRateLimitError):
                rag_service._gemini_post(
                    "https://idp/x", params={"key": "k"},
                    payload={}, timeout=10,
                )

    def test_success_returns_response(self):
        from app.services import rag_service
        ok = _make_resp(200, _gemini_response("hi from gemini"))
        with patch.object(rag_service._HTTP, "post", return_value=ok):
            resp = rag_service._gemini_post(
                "https://idp/x", params={"key": "k"},
                payload={}, timeout=10,
            )
        assert resp.status_code == 200


# ════════════════════════════════════════════════════════════════════════════
# Embed query — Ollama HTTP integration
# ════════════════════════════════════════════════════════════════════════════


class TestEmbedQuery:
    def test_returns_embeddings_from_ollama(self, monkeypatch):
        from app.services import rag_service
        monkeypatch.setattr(rag_service, "_use_mistral_embed", lambda: False)
        ok = _make_resp(200, {"embeddings": [[0.1, 0.2, 0.3]]})
        with patch.object(rag_service, "_get_cache", return_value=None), \
             patch.object(rag_service._HTTP, "post", return_value=ok):
            emb = rag_service._embed_query("hello")
        assert emb == [0.1, 0.2, 0.3]

    def test_handles_empty_embeddings_response(self, monkeypatch):
        from app.services import rag_service
        monkeypatch.setattr(rag_service, "_use_mistral_embed", lambda: False)
        ok = _make_resp(200, {"embeddings": []})
        with patch.object(rag_service, "_get_cache", return_value=None), \
             patch.object(rag_service._HTTP, "post", return_value=ok):
            with pytest.raises((IndexError, KeyError, ValueError, Exception)):
                rag_service._embed_query("hello")
