"""Tests for per-user BYOK (bring-your-own-key): model, resolution, routing.

Each user supplies their own LLM key; rag_service._chat resolves the user's
own key first, then the platform Mistral key (for any authenticated user when
one is configured), with NO Ollama fallback off-sovereign (sovereign is
air-gap Ollama by design).
"""
import uuid

from app.utils.crypto import encrypt_token


def _mk_user(db, **kwargs):
    from app.models.user import User
    u = User(email=f"byok-{uuid.uuid4().hex[:8]}@example.com", **kwargs)
    u.set_password("Byok1234!")
    db.session.add(u)
    db.session.commit()
    return u


# --- User.has_ai_key property ----------------------------------------

class TestHasAiKeyProperty:
    def test_false_without_anything(self):
        from app.models.user import User
        assert User(email="x@example.com").has_ai_key is False

    def test_false_with_provider_but_no_key(self):
        from app.models.user import User
        assert User(email="x@example.com", ai_provider="claude").has_ai_key is False

    def test_true_with_provider_and_key(self):
        from app.models.user import User
        u = User(email="x@example.com", ai_provider="claude", ai_api_key_enc="enc")
        assert u.has_ai_key is True


# --- _resolve_byok priority + Pro gate on platform fallback ----------

class TestResolveByok:
    def test_none_when_no_key_and_no_platform(self, app, monkeypatch):
        from app.services import rag_service
        monkeypatch.setattr(rag_service, "_use_mistral_api", lambda: False)
        assert rag_service._resolve_byok(None) is None

    def test_no_platform_fallback_without_user(self, app, monkeypatch):
        # Platform Mistral fallback is gated on a Pro user; with no user_id
        # there is no one to check is_pro_subscriber on -> None.
        from app.services import rag_service
        monkeypatch.setattr(rag_service, "_use_mistral_api", lambda: True)
        monkeypatch.setattr(rag_service, "_MISTRAL_API_KEY", "platform-key")
        assert rag_service._resolve_byok(None) is None

    def test_platform_mistral_for_pro_user(self, app, db, monkeypatch):
        # A Pro user with no own key falls back to the platform Mistral key.
        from app.services import rag_service
        monkeypatch.setattr(rag_service, "_use_mistral_api", lambda: True)
        monkeypatch.setattr(rag_service, "_MISTRAL_API_KEY", "platform-key")
        monkeypatch.setattr(rag_service, "_MISTRAL_LLM_MODEL", "mistral-medium-latest")
        u = _mk_user(db, abonnement_type="premium_pro", is_confirmed=True)
        try:
            provider, key, model, source = rag_service._resolve_byok(u.id)
            assert provider == "mistral"
            assert key == "platform-key"
            assert source == "platform"
        finally:
            db.session.delete(u)
            db.session.commit()

    def test_user_key_wins_over_platform(self, app, db, monkeypatch):
        # Even a Free user's OWN key takes priority over (and unlocks) synthesis.
        from app.services import rag_service
        monkeypatch.setattr(rag_service, "_use_mistral_api", lambda: True)
        u = _mk_user(db, ai_provider="claude", ai_api_key_enc=encrypt_token("sk-user"))
        try:
            provider, key, model, source = rag_service._resolve_byok(u.id)
            assert provider == "claude"
            assert key == "sk-user"
            assert source == f"user:{u.id}"
        finally:
            db.session.delete(u)
            db.session.commit()

    def test_user_default_model_when_unset(self, app, db, monkeypatch):
        from app.services import rag_service
        monkeypatch.setattr(rag_service, "_use_mistral_api", lambda: False)
        u = _mk_user(db, ai_provider="openai", ai_api_key_enc=encrypt_token("sk-x"))
        try:
            _, _, model, _ = rag_service._resolve_byok(u.id)
            assert model == rag_service._AI_PROVIDER_DEFAULTS["openai"]
        finally:
            db.session.delete(u)
            db.session.commit()


# --- can_summarize_for_user gate (digest summary eligibility) --------

class TestCanSummarizeForUser:
    def test_true_with_own_key(self, app):
        # Any user with their own key can summarise — even Free.
        from app.models.user import User
        from app.services import rag_service
        u = User(email="x@example.com", ai_provider="claude",
                 ai_api_key_enc="enc", abonnement_type="gratuit")
        assert rag_service.can_summarize_for_user(u) is True

    def test_true_for_pro_with_platform(self, app, db, monkeypatch):
        # Pro user, no own key, platform Mistral available -> True.
        from app.services import rag_service
        monkeypatch.setattr(rag_service, "_use_mistral_api", lambda: True)
        u = _mk_user(db, abonnement_type="premium_pro", is_confirmed=True)
        try:
            assert rag_service.can_summarize_for_user(u) is True
        finally:
            db.session.delete(u)
            db.session.commit()

    def test_false_when_no_platform_and_no_key(self, app, db, monkeypatch):
        from app.services import rag_service
        monkeypatch.setattr(rag_service, "_use_mistral_api", lambda: False)
        u = _mk_user(db, abonnement_type="premium_pro", is_confirmed=True)
        try:
            assert rag_service.can_summarize_for_user(u) is False
        finally:
            db.session.delete(u)
            db.session.commit()


# --- _chat routing (pure BYOK, no Ollama fallback off-sovereign) -----

class TestChatByokRouting:
    def test_no_key_returns_configure_message(self, app, monkeypatch):
        from app.services import rag_service
        monkeypatch.setattr("app.utils.deployment_mode.is_sovereign", lambda: False)
        monkeypatch.setattr(rag_service, "_use_mistral_api", lambda: False)
        answer, warning = rag_service._chat("hi", user_id=None)
        assert "clé" in answer
        assert warning

    def test_pro_user_no_key_uses_platform_mistral(self, app, db, monkeypatch):
        from app.services import rag_service
        monkeypatch.setattr("app.utils.deployment_mode.is_sovereign", lambda: False)
        monkeypatch.setattr(rag_service, "_use_mistral_api", lambda: True)
        monkeypatch.setattr(rag_service, "_MISTRAL_API_KEY", "platform-key")
        captured = {}

        def fake_dispatch(provider, msg, key, model, max_tokens, sp):
            captured.update(provider=provider, key=key)
            return "PLATFORM"

        monkeypatch.setattr(rag_service, "_dispatch_provider", fake_dispatch)
        u = _mk_user(db, abonnement_type="premium_pro", is_confirmed=True)
        try:
            answer, warning = rag_service._chat("hi", user_id=u.id)
        finally:
            db.session.delete(u)
            db.session.commit()
        assert answer == "PLATFORM"
        assert warning is None
        assert captured == {"provider": "mistral", "key": "platform-key"}

    def test_user_key_routes_to_its_provider(self, app, db, monkeypatch):
        from app.services import rag_service
        monkeypatch.setattr("app.utils.deployment_mode.is_sovereign", lambda: False)
        captured = {}

        def fake_dispatch(provider, msg, key, model, max_tokens, sp):
            captured.update(provider=provider, key=key, model=model)
            return "ANSWER"

        monkeypatch.setattr(rag_service, "_dispatch_provider", fake_dispatch)
        u = _mk_user(
            db, ai_provider="openai", ai_model="gpt-4o",
            ai_api_key_enc=encrypt_token("sk-xyz"),
        )
        try:
            answer, warning = rag_service._chat("hi", user_id=u.id)
        finally:
            db.session.delete(u)
            db.session.commit()
        assert answer == "ANSWER"
        assert warning is None
        assert captured == {"provider": "openai", "key": "sk-xyz", "model": "gpt-4o"}

    def test_provider_error_surfaces_no_ollama_fallback(self, app, db, monkeypatch):
        from app.services import rag_service
        monkeypatch.setattr("app.utils.deployment_mode.is_sovereign", lambda: False)

        def boom(*a, **k):
            raise RuntimeError("provider down")

        def fail_ollama(*a, **k):
            raise AssertionError("Ollama must not be used in pure BYOK")

        monkeypatch.setattr(rag_service, "_dispatch_provider", boom)
        monkeypatch.setattr(rag_service, "_chat_ollama", fail_ollama)
        u = _mk_user(db, ai_provider="claude", ai_api_key_enc=encrypt_token("sk-x"))
        try:
            answer, warning = rag_service._chat("hi", user_id=u.id)
        finally:
            db.session.delete(u)
            db.session.commit()
        assert "Impossible de générer" in answer
        assert "RuntimeError" in warning

    def test_sovereign_uses_ollama(self, app, monkeypatch):
        from app.services import rag_service
        monkeypatch.setattr("app.utils.deployment_mode.is_sovereign", lambda: True)
        monkeypatch.setattr(
            rag_service, "_chat_ollama",
            lambda msg, mt, system_prompt=None: "LOCAL",
        )
        answer, warning = rag_service._chat("hi", user_id=123)
        assert answer == "LOCAL"
        assert warning is None


# --- summarize_for_user (per-user digest summary, BYOK) --------------

class TestSummarizeForUser:
    def test_none_without_key(self, app, monkeypatch):
        from app.services import rag_service
        monkeypatch.setattr(rag_service, "_use_mistral_api", lambda: False)
        assert rag_service.summarize_for_user("some text", user_id=None) is None

    def test_none_for_empty_text(self, app):
        from app.services import rag_service
        assert rag_service.summarize_for_user("   ", user_id=1) is None

    def test_uses_user_key_and_summary_prompt(self, app, db, monkeypatch):
        from app.services import rag_service
        captured = {}

        def fake_dispatch(provider, msg, key, model, max_tokens, sp):
            captured.update(provider=provider, key=key, sp=sp)
            return "  SUMMARY  "

        monkeypatch.setattr(rag_service, "_dispatch_provider", fake_dispatch)
        u = _mk_user(db, ai_provider="claude", ai_api_key_enc=encrypt_token("sk-c"))
        try:
            out = rag_service.summarize_for_user("article body", user_id=u.id)
        finally:
            db.session.delete(u)
            db.session.commit()
        assert out == "SUMMARY"  # stripped
        assert captured["provider"] == "claude"
        assert captured["key"] == "sk-c"
        assert captured["sp"] == rag_service._SUMMARY_SYSTEM_PROMPT

    def test_provider_error_returns_none(self, app, db, monkeypatch):
        from app.services import rag_service

        def boom(*a, **k):
            raise RuntimeError("provider down")

        monkeypatch.setattr(rag_service, "_dispatch_provider", boom)
        u = _mk_user(db, ai_provider="openai", ai_api_key_enc=encrypt_token("sk-o"))
        try:
            assert rag_service.summarize_for_user("body", user_id=u.id) is None
        finally:
            db.session.delete(u)
            db.session.commit()


class TestAttachSummaries:
    def test_attaches_and_caps(self, app, monkeypatch):
        from app.cron import send_recap_digests as cron
        from app.services import rag_service

        monkeypatch.setattr(rag_service, "summarize_for_user",
                            lambda content, user_id: f"sum:{content}")
        n = cron._MAX_SUMMARIES_PER_DIGEST + 3
        articles = [{"content": f"c{i}"} for i in range(n)]
        cron._attach_summaries(user_id=1, articles=articles)
        assert articles[0]["summary"] == "sum:c0"
        assert all("summary" in a for a in articles[:cron._MAX_SUMMARIES_PER_DIGEST])
        assert "summary" not in articles[-1]

    def test_failed_summary_left_absent(self, app, monkeypatch):
        from app.cron import send_recap_digests as cron
        from app.services import rag_service
        monkeypatch.setattr(rag_service, "summarize_for_user",
                            lambda content, user_id: None)
        articles = [{"content": "c0"}]
        cron._attach_summaries(user_id=1, articles=articles)
        assert "summary" not in articles[0]


# --- per-user embed BYOK resolution (Phase C: €0 indexing on Free) ---

class TestResolveEmbedForUser:
    def _clear(self):
        from app.services import rag_service
        rag_service._resolve_embed_for_user_cached.cache_clear()

    def test_none_without_any_key(self, app, db):
        from app.services import rag_service
        self._clear()
        u = _mk_user(db, abonnement_type="gratuit")
        try:
            assert rag_service._resolve_embed_for_user(u.id) is None
        finally:
            db.session.delete(u)
            db.session.commit()

    def test_derives_from_mistral_synthesis_key(self, app, db):
        # A single Mistral key powers both synthesis AND embedding on Free.
        from app.services import rag_service
        self._clear()
        u = _mk_user(db, ai_provider="mistral", ai_api_key_enc=encrypt_token("sk-mi"))
        try:
            cfg = rag_service._resolve_embed_for_user(u.id)
            assert cfg["provider"] == "mistral"
            assert cfg["api_key"] == "sk-mi"
            assert cfg["model"] == rag_service._BYOK_EMBED_DEFAULT_MODEL["mistral"]
        finally:
            db.session.delete(u)
            db.session.commit()

    def test_none_for_claude_synthesis_key(self, app, db):
        # Claude has no embedding API used here → no derived embed config.
        from app.services import rag_service
        self._clear()
        u = _mk_user(db, ai_provider="claude", ai_api_key_enc=encrypt_token("sk-cl"))
        try:
            assert rag_service._resolve_embed_for_user(u.id) is None
        finally:
            db.session.delete(u)
            db.session.commit()

    def test_explicit_embed_key_wins_over_synthesis(self, app, db):
        from app.services import rag_service
        self._clear()
        u = _mk_user(db, ai_provider="claude", ai_api_key_enc=encrypt_token("sk-cl"),
                     embed_provider="openai", embed_api_key_enc=encrypt_token("sk-emb"))
        try:
            cfg = rag_service._resolve_embed_for_user(u.id)
            assert cfg["provider"] == "openai"
            assert cfg["api_key"] == "sk-emb"
        finally:
            db.session.delete(u)
            db.session.commit()

    def test_derives_from_gemini_synthesis_key(self, app, db):
        # Gemini has an embedding API (text-embedding-004) → a Gemini key
        # powers both answers and indexing.
        from app.services import rag_service
        self._clear()
        u = _mk_user(db, ai_provider="gemini", ai_api_key_enc=encrypt_token("sk-gm"))
        try:
            cfg = rag_service._resolve_embed_for_user(u.id)
            assert cfg["provider"] == "gemini"
            assert cfg["model"] == "text-embedding-004"
            assert "gemini" in rag_service._BYOK_EMBED_BACKENDS
        finally:
            db.session.delete(u)
            db.session.commit()


class TestPlatformEmbedAllowed:
    def test_system_user_none_allowed(self, app, monkeypatch):
        from app.services import rag_service
        monkeypatch.setattr("app.utils.deployment_mode.is_sovereign", lambda: False)
        assert rag_service._platform_embed_allowed(None) is True

    def test_pro_user_allowed(self, app, db, monkeypatch):
        from app.services import rag_service
        monkeypatch.setattr("app.utils.deployment_mode.is_sovereign", lambda: False)
        u = _mk_user(db, abonnement_type="premium_pro", is_confirmed=True)
        try:
            assert rag_service._platform_embed_allowed(u.id) is True
        finally:
            db.session.delete(u)
            db.session.commit()

    def test_sovereign_always_allowed(self, app, db, monkeypatch):
        from app.services import rag_service
        monkeypatch.setattr("app.utils.deployment_mode.is_sovereign", lambda: True)
        u = _mk_user(db, abonnement_type="gratuit", is_confirmed=True)
        try:
            assert rag_service._platform_embed_allowed(u.id) is True
        finally:
            db.session.delete(u)
            db.session.commit()
