"""Coverage for app/services/rag_service.py — _chat router, _rerank, search, newsletter pipeline.

Complements test_rag_service_units.py and test_rag_service_indexing.py.
This file focuses on the orchestration layer (search pipeline, LLM
provider routing, reranking) with everything external mocked.
"""
import uuid
from unittest.mock import MagicMock, patch


def _fake_collection(*, name="test", count=0, query_result=None):
    col = MagicMock()
    col.name = name
    col.metadata = {"hnsw:search_ef": 200}
    col.count.return_value = count
    if query_result is None:
        query_result = {
            "ids": [[]], "documents": [[]],
            "metadatas": [[]], "distances": [[]],
        }
    col.query.return_value = query_result
    return col


# ─────────────────────────────────────────────────────────────────
# _chat() router — sovereign mode + org provider + fallback chain
# ─────────────────────────────────────────────────────────────────

def _mk_pro_user(db, app):
    """Create a Pro subscriber (is_pro_subscriber True), return its id."""
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


class TestChatRouter:
    """Pivot pure BYOK off-sovereign: user key → platform Mistral
    (only when a platform key is configured) → "configure your key"
    message. NO Ollama / cross-provider fallback off-sovereign;
    sovereign short-circuits to local Ollama."""

    def test_sovereign_mode_uses_ollama_only(self, monkeypatch):
        from app.services import rag_service
        monkeypatch.setenv("SOVEREIGN_MODE", "true")
        with patch.object(rag_service, "_chat_ollama", return_value="local") as ollama:
            answer, warn = rag_service._chat("question")
        assert answer == "local"
        assert warn is None
        ollama.assert_called_once()

    def test_pro_user_no_key_uses_platform_mistral(self, app, db, monkeypatch):
        from app.services import rag_service
        monkeypatch.setenv("SOVEREIGN_MODE", "false")
        monkeypatch.setattr(rag_service, "_use_mistral_api", lambda: True)
        monkeypatch.setattr(rag_service, "_MISTRAL_API_KEY", "platform-key")
        uid = _mk_pro_user(db, app)
        with app.app_context():
            with patch.object(rag_service, "_chat_mistral",
                              return_value="from mistral") as mst, \
                 patch.object(rag_service, "_chat_ollama") as oll:
                answer, warn = rag_service._chat("q", user_id=uid)
        assert answer == "from mistral"
        assert warn is None
        assert mst.called
        oll.assert_not_called()

    def test_no_key_no_platform_returns_configure_message(self, app, db, monkeypatch):
        from app.services import rag_service
        monkeypatch.setenv("SOVEREIGN_MODE", "false")
        monkeypatch.setattr(rag_service, "_use_mistral_api", lambda: False)
        monkeypatch.setattr(rag_service, "_MISTRAL_API_KEY", "")
        uid = _mk_pro_user(db, app)
        with app.app_context():
            with patch.object(rag_service, "_chat_ollama") as oll:
                answer, warn = rag_service._chat("q", user_id=uid)
        assert "clé" in answer
        assert warn is not None
        # No silent Ollama fallback off-sovereign.
        oll.assert_not_called()

    def test_mistral_rate_limited_surfaces_quota_no_ollama(self, app, db, monkeypatch):
        from app.services import rag_service
        monkeypatch.setenv("SOVEREIGN_MODE", "false")
        monkeypatch.setattr(rag_service, "_use_mistral_api", lambda: True)
        monkeypatch.setattr(rag_service, "_MISTRAL_API_KEY", "platform-key")
        uid = _mk_pro_user(db, app)
        with app.app_context():
            with patch.object(rag_service, "_chat_mistral",
                              side_effect=rag_service._MistralRateLimitError("429")), \
                 patch.object(rag_service, "_chat_ollama") as oll:
                answer, warn = rag_service._chat("q", user_id=uid)
        assert warn is not None
        assert "Mistral" in answer or "quota" in warn.lower()
        oll.assert_not_called()

    def test_provider_other_exception_surfaces_no_ollama(self, app, db, monkeypatch):
        from app.services import rag_service
        monkeypatch.setenv("SOVEREIGN_MODE", "false")
        monkeypatch.setattr(rag_service, "_use_mistral_api", lambda: True)
        monkeypatch.setattr(rag_service, "_MISTRAL_API_KEY", "platform-key")
        uid = _mk_pro_user(db, app)
        with app.app_context():
            with patch.object(rag_service, "_chat_mistral",
                              side_effect=RuntimeError("server 500")), \
                 patch.object(rag_service, "_chat_ollama") as oll:
                answer, warn = rag_service._chat("q", user_id=uid)
        # The new contract surfaces provider errors — no silent fallback.
        assert "Impossible de générer" in answer
        assert warn is not None
        oll.assert_not_called()


# ─────────────────────────────────────────────────────────────────
# _chat_ollama
# ─────────────────────────────────────────────────────────────────

class TestChatOllama:
    def test_returns_response_from_ollama_chat_endpoint(self):
        from app.services import rag_service
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "message": {"content": "ollama answer"},
        }
        with patch.object(rag_service._HTTP, "post",
                          return_value=mock_resp) as p:
            out = rag_service._chat_ollama("question", max_tokens=100)
        assert "ollama answer" in out
        called_url = p.call_args.args[0] if p.call_args.args else p.call_args.kwargs.get("url", "")
        assert "/api/chat" in called_url


# ─────────────────────────────────────────────────────────────────
# _rerank
# ─────────────────────────────────────────────────────────────────

class TestRerank:
    def test_skips_rerank_when_pool_smaller_than_top_k(self):
        from app.services import rag_service
        rows = [("a", {}, 0.1, 1.0), ("b", {}, 0.2, 0.9)]
        out = rag_service._rerank("q", rows, top_k=10)
        assert out == rows  # short-circuit, no LLM call

    def test_parses_comma_separated_indices(self):
        from app.services import rag_service
        rows = [
            ("doc 1", {}, 0.1, 1.0),
            ("doc 2", {}, 0.2, 0.9),
            ("doc 3", {}, 0.3, 0.8),
            ("doc 4", {}, 0.4, 0.7),
        ]
        # LLM says "3, 1" — pick docs at indices 2 and 0
        with patch.object(rag_service, "_llm_generate",
                          return_value="3, 1"):
            out = rag_service._rerank("q", rows, top_k=2)
        assert out[0][0] == "doc 3"
        assert out[1][0] == "doc 1"

    def test_falls_back_to_rrf_order_when_llm_returns_garbage(self):
        from app.services import rag_service
        rows = [
            ("doc 1", {}, 0.1, 1.0),
            ("doc 2", {}, 0.2, 0.9),
            ("doc 3", {}, 0.3, 0.8),
        ]
        # No numbers → ordered list stays empty → fallback to rows[:top_k]
        with patch.object(rag_service, "_llm_generate",
                          return_value="this is not numbers"):
            out = rag_service._rerank("q", rows, top_k=2)
        assert out == rows[:2]

    def test_falls_back_on_llm_exception(self):
        from app.services import rag_service
        rows = [
            ("doc 1", {}, 0.1, 1.0),
            ("doc 2", {}, 0.2, 0.9),
            ("doc 3", {}, 0.3, 0.8),
        ]
        with patch.object(rag_service, "_llm_generate",
                          side_effect=Exception("LLM down")):
            out = rag_service._rerank("q", rows, top_k=2)
        assert out == rows[:2]

    def test_partial_rerank_filled_from_rrf(self):
        """LLM returns only 1 valid index, top_k=2 → fill remaining slot from RRF order."""
        from app.services import rag_service
        rows = [
            ("doc 1", {}, 0.1, 1.0),
            ("doc 2", {}, 0.2, 0.9),
            ("doc 3", {}, 0.3, 0.8),
            ("doc 4", {}, 0.4, 0.7),
        ]
        # LLM picks doc 3 (index 2) only — top_k=2 → fill 1 slot from RRF
        with patch.object(rag_service, "_llm_generate", return_value="3"):
            out = rag_service._rerank("q", rows, top_k=2)
        assert out[0][0] == "doc 3"
        assert len(out) == 2
        # Slot 2 filled from RRF order, skipping idx=2 already chosen
        assert out[1][0] == "doc 1"

    def test_dedupes_repeated_indices(self):
        from app.services import rag_service
        rows = [
            ("doc 1", {}, 0.1, 1.0),
            ("doc 2", {}, 0.2, 0.9),
            ("doc 3", {}, 0.3, 0.8),
        ]
        # LLM repeats "1, 1, 2" — dedup to [0, 1]
        with patch.object(rag_service, "_llm_generate", return_value="1, 1, 2"):
            out = rag_service._rerank("q", rows, top_k=2)
        assert out[0][0] == "doc 1"
        assert out[1][0] == "doc 2"


# ─────────────────────────────────────────────────────────────────
# index_newsletter / clear_index / index_all_newsletters
# ─────────────────────────────────────────────────────────────────

class TestIndexNewsletter:
    def test_unknown_newsletter_returns_zero(self, app, db):
        from app.services import rag_service
        with app.app_context():
            n = rag_service.index_newsletter(99999)
        assert n == 0

    def test_skips_too_short_sections(self, app, db):
        from datetime import date

        from app.models.newsletter import Newsletter
        from app.models.newsletter_section import NewsletterSection
        from app.services import rag_service

        with app.app_context():
            nl = Newsletter(title="Test NL", contenu="<p>body</p>", date=date(2025, 1, 1), status="sent")
            db.session.add(nl)
            db.session.flush()
            sec = NewsletterSection(
                newsletter_id=nl.id,
                title="Tiny", content="x", tag="",
            )
            db.session.add(sec)
            db.session.commit()
            nl_id = nl.id

            n = rag_service.index_newsletter(nl_id)
            # Section content too short (< 30 chars) → skipped
            assert n == 0

            db.session.delete(db.session.get(NewsletterSection, sec.id))
            db.session.delete(db.session.get(Newsletter, nl_id))
            db.session.commit()

    def test_indexes_long_sections(self, app, db):
        from datetime import date

        from app.models.newsletter import Newsletter
        from app.models.newsletter_section import NewsletterSection
        from app.services import rag_service

        with app.app_context():
            nl = Newsletter(title="NL", contenu="<p>full content</p>", date=date(2025, 1, 1), status="sent")
            db.session.add(nl)
            db.session.flush()
            sec = NewsletterSection(
                newsletter_id=nl.id,
                title="Real",
                content="<p>" + "Substantial content here. " * 10 + "</p>",
                tag="ai",
            )
            db.session.add(sec)
            db.session.commit()
            nl_id = nl.id
            sec_id = sec.id

            col = _fake_collection()
            with patch.object(rag_service, "_embed",
                              return_value=[[0.1] * 5]):
                with patch.object(rag_service, "_get_collection",
                                  return_value=col):
                    n = rag_service.index_newsletter(nl_id)
            assert n == 1
            col.upsert.assert_called_once()
            kwargs = col.upsert.call_args.kwargs
            assert kwargs["ids"] == [f"section_{sec_id}"]

            db.session.delete(db.session.get(NewsletterSection, sec_id))
            db.session.delete(db.session.get(Newsletter, nl_id))
            db.session.commit()

    def test_chroma_failure_returns_zero(self, app, db):
        from datetime import date

        from app.models.newsletter import Newsletter
        from app.models.newsletter_section import NewsletterSection
        from app.services import rag_service

        with app.app_context():
            nl = Newsletter(title="NL", contenu="<p>raw</p>", date=date(2025, 1, 1), status="sent")
            db.session.add(nl)
            db.session.flush()
            sec = NewsletterSection(
                newsletter_id=nl.id, title="S",
                content="<p>" + "Body content. " * 10 + "</p>",
            )
            db.session.add(sec)
            db.session.commit()
            nl_id = nl.id
            sec_id = sec.id

            col = _fake_collection()
            col.upsert.side_effect = RuntimeError("chroma down")
            with patch.object(rag_service, "_embed", return_value=[[0.1] * 5]):
                with patch.object(rag_service, "_get_collection", return_value=col):
                    n = rag_service.index_newsletter(nl_id)
            assert n == 0

            db.session.delete(db.session.get(NewsletterSection, sec_id))
            db.session.delete(db.session.get(Newsletter, nl_id))
            db.session.commit()


class TestClearIndex:
    def test_deletes_then_recreates_collection(self):
        from app.services import rag_service

        client = MagicMock()
        with patch("chromadb.PersistentClient", return_value=client):
            rag_service.clear_index()

        client.delete_collection.assert_called_once()
        client.get_or_create_collection.assert_called_once()

    def test_handles_missing_collection_gracefully(self):
        from app.services import rag_service

        client = MagicMock()
        client.delete_collection.side_effect = Exception("not found")
        with patch("chromadb.PersistentClient", return_value=client):
            # Must not raise
            rag_service.clear_index()
        # Recreation still attempted
        client.get_or_create_collection.assert_called_once()


class TestIndexAllNewsletters:
    def test_iterates_published_newsletters(self, app, db):
        from datetime import date

        from app.models.newsletter import Newsletter
        from app.services import rag_service

        with app.app_context():
            nl1 = Newsletter(title="NL1", contenu="x", date=date(2025, 1, 1), status="sent")
            nl2 = Newsletter(title="NL2", contenu="y", date=date(2025, 1, 2), status="sent")
            db.session.add_all([nl1, nl2])
            db.session.commit()

            with patch.object(rag_service, "index_newsletter",
                              side_effect=[3, 5]) as inj:
                result = rag_service.index_all_newsletters(clear_first=False)

            assert inj.call_count == 2
            assert result == {"newsletters": 2, "sections": 8}

            db.session.query(Newsletter).filter(
                Newsletter.id.in_([nl1.id, nl2.id])
            ).delete(synchronize_session=False)
            db.session.commit()

    def test_clear_first_calls_clear_index(self, app, db):
        from app.services import rag_service

        with app.app_context():
            with patch.object(rag_service, "clear_index") as ci:
                with patch.object(rag_service, "index_newsletter",
                                  return_value=0):
                    rag_service.index_all_newsletters(clear_first=True)
            ci.assert_called_once()


# ─────────────────────────────────────────────────────────────────
# search() — main entry point
# ─────────────────────────────────────────────────────────────────

class TestSearch:
    def test_no_results_returns_friendly_empty_message(self, app):
        from app.services import rag_service

        col = _fake_collection(count=0)
        with app.app_context():
            with patch.object(rag_service, "_get_cache", return_value=None):
                with patch.object(rag_service, "_embed_query",
                                  return_value=[0.1, 0.2]):
                    with patch.object(rag_service, "_get_collection", return_value=col):
                        out = rag_service.search("hello world", scope="shared")
        assert out["sources"] == []
        assert "résultat" in out["answer"].lower() or "result" in out["answer"].lower()

    def test_personal_scope_no_results_returns_specific_message(self, app):
        from app.services import rag_service

        col = _fake_collection(count=0)
        with app.app_context():
            with patch.object(rag_service, "_get_cache", return_value=None):
                with patch.object(rag_service, "_embed_query",
                                  return_value=[0.1, 0.2]):
                    with patch.object(rag_service, "_get_user_collection",
                                      return_value=col):
                        out = rag_service.search("q", scope="personal", user_id=1)
        assert out["sources"] == []
        assert "bibliothèque" in out["answer"].lower() or "personal" in out["answer"].lower()

    def test_ollama_unavailable_returns_user_friendly_error(self, app):
        import requests

        from app.services import rag_service

        with app.app_context():
            with patch.object(rag_service, "_get_cache", return_value=None):
                with patch.object(rag_service, "_should_use_hyde", return_value=False):
                    with patch.object(rag_service, "_embed_query",
                                      side_effect=requests.RequestException("conn refused")):
                        out = rag_service.search("q")
        assert out["sources"] == []
        assert "indispon" in out["answer"].lower()

    def test_cache_hit_returns_cached_result(self, app):
        import json as _json

        from app.services import rag_service

        cached = {"answer": "cached answer", "sources": [{"num": 1}]}
        cache = MagicMock()
        cache.get.return_value = _json.dumps(cached)

        with app.app_context():
            with patch.object(rag_service, "_get_cache", return_value=cache):
                out = rag_service.search("q", scope="shared")
        assert out == cached

    def test_full_pipeline_with_one_result(self, app):
        from app.services import rag_service

        # ChromaDB returns 1 row
        col = _fake_collection(count=1, query_result={
            "ids": [["section_1"]],
            "documents": [["the answer is in this document"]],
            "metadatas": [[{
                "source_type": "newsletter",
                "section_title": "AI", "newsletter_title": "Weekly",
                "newsletter_date": "2025-01-15",
            }]],
            "distances": [[0.2]],
        })
        with app.app_context():
            with patch.object(rag_service, "_get_cache", return_value=None):
                with patch.object(rag_service, "_should_use_hyde", return_value=False):
                    with patch.object(rag_service, "_embed_query",
                                      return_value=[0.1, 0.2]):
                        with patch.object(rag_service, "_get_collection",
                                          return_value=col):
                            with patch.object(rag_service, "_chat",
                                              return_value=("synth answer", None)):
                                with patch.object(rag_service, "_RERANK_ENABLED", False):
                                    out = rag_service.search("simple question")
        assert out["answer"] == "synth answer"
        assert len(out["sources"]) >= 1
        assert out["sources"][0]["source_type"] == "newsletter"

    def test_search_returns_ai_warning_on_provider_failure(self, app):
        from app.services import rag_service

        col = _fake_collection(count=1, query_result={
            "ids": [["section_1"]],
            "documents": [["body"]],
            "metadatas": [[{"source_type": "newsletter", "section_title": "S"}]],
            "distances": [[0.1]],
        })
        with app.app_context():
            with patch.object(rag_service, "_get_cache", return_value=None):
                with patch.object(rag_service, "_should_use_hyde", return_value=False):
                    with patch.object(rag_service, "_embed_query",
                                      return_value=[0.1]):
                        with patch.object(rag_service, "_get_collection",
                                          return_value=col):
                            with patch.object(
                                rag_service, "_chat",
                                return_value=("answer", "Provider X failed"),
                            ):
                                with patch.object(rag_service, "_RERANK_ENABLED", False):
                                    out = rag_service.search("q")
        assert out.get("ai_warning") == "Provider X failed"

    def test_on_stage_callback_invoked_in_pipeline_order(self, app):
        """Progress callback receives the four canonical stages in order.

        embed → retrieve → (rerank if enabled) → synth. With reranking
        disabled the rerank stage is skipped so we only require the
        three remaining stages.
        """
        from app.services import rag_service

        col = _fake_collection(query_result={
            "ids": [["c1"]],
            "documents": [["doc"]],
            "metadatas": [[{"source_type": "newsletter"}]],
            "distances": [[0.2]],
        })
        seen: list[str] = []

        with app.app_context():
            with patch.object(rag_service, "_get_cache", return_value=None):
                with patch.object(rag_service, "_embed_query", return_value=[0.1]):
                    with patch.object(rag_service, "_get_collection", return_value=col):
                        with patch.object(rag_service, "_chat", return_value=("ok", None)):
                            with patch.object(rag_service, "_RERANK_ENABLED", False):
                                rag_service.search("q", on_stage=seen.append)

        # Order matters: the UI relies on this sequence to drive the
        # milestone label. retrieve appears at least once; synth always
        # closes the run.
        assert seen[0] == "embed"
        assert "retrieve" in seen
        assert seen[-1] == "synth"
        assert "rerank" not in seen  # rerank disabled in this test

    def test_on_stage_callback_failure_is_swallowed(self, app):
        """A buggy on_stage observer must never break a search."""
        from app.services import rag_service

        col = _fake_collection(query_result={
            "ids": [["c1"]],
            "documents": [["doc"]],
            "metadatas": [[{"source_type": "newsletter"}]],
            "distances": [[0.2]],
        })

        def boom(_stage):
            raise RuntimeError("observer crashed")

        with app.app_context():
            with patch.object(rag_service, "_get_cache", return_value=None):
                with patch.object(rag_service, "_embed_query", return_value=[0.1]):
                    with patch.object(rag_service, "_get_collection", return_value=col):
                        with patch.object(rag_service, "_chat", return_value=("ok", None)):
                            with patch.object(rag_service, "_RERANK_ENABLED", False):
                                out = rag_service.search("q", on_stage=boom)
        assert out.get("answer") == "ok"


# ─────────────────────────────────────────────────────────────────
# _hybrid_query_collection — sparse pass with keywords
# ─────────────────────────────────────────────────────────────────

class TestHybridQueryWithKeywords:
    def test_sparse_pass_runs_when_keywords_present(self):
        from app.services import rag_service

        # Both dense and sparse passes use the same fake col.query mock
        col = _fake_collection(count=10, query_result={
            "ids": [["d1"]],
            "documents": [["doc one"]],
            "metadatas": [[{"x": 1}]],
            "distances": [[0.1]],
        })
        with patch.object(rag_service, "_extract_keywords",
                          return_value=["python", "rag"]):
            d, m, dist, rrf = rag_service._hybrid_query_collection(
                col, [0.1], "Python and RAG architecture", n=5,
            )
        # query() called multiple times: dense + 2 keywords
        assert col.query.call_count >= 3
        assert len(d) >= 1


# ─────────────────────────────────────────────────────────────────
# Specific helper coverage gaps
# ─────────────────────────────────────────────────────────────────

class TestResolvedLocalModel:
    def test_returns_string(self):
        from app.services import rag_service
        result = rag_service._resolved_local_model()
        # Returns the configured Ollama LLM model (may be empty when none set
        # — fallback handled at call-site, not here).
        assert isinstance(result, str)
