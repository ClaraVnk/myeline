"""Coverage for app/services/rag_service.py — indexing + retrieval logic.

Complements test_rag_service_units.py (pure helpers + HTTP retry).
This file mocks chromadb so we can exercise the full indexing /
retrieval / source-formatting paths without bringing up a real vector
store.
"""
import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────
# Helpers — fake ChromaDB collection
# ─────────────────────────────────────────────────────────────────

def _fake_collection(*, name="test", count=0, query_result=None, get_result=None):
    """Build a MagicMock that behaves like a ChromaDB collection."""
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
    if get_result is None:
        get_result = {"ids": [], "metadatas": []}
    col.get.return_value = get_result
    return col


# ─────────────────────────────────────────────────────────────────
# Collection helpers — _get_collection / _get_user_collection / _get_org_collection
# ─────────────────────────────────────────────────────────────────

class TestCollectionHelpers:
    """The chroma client is now cached at module scope (perf audit M14),
    so patching `chromadb.PersistentClient` doesn't take effect after the
    first test run. Patch `rag_service._chroma_client` directly instead —
    it returns the cached instance and is the right seam to mock.
    """

    def test_get_collection_calls_chromadb(self):
        from app.services import rag_service
        fake_client = MagicMock()
        fake_col = _fake_collection()
        fake_client.get_or_create_collection.return_value = fake_col
        with patch.object(rag_service, "_chroma_client", return_value=fake_client):
            col = rag_service._get_collection()
        assert col is fake_col
        fake_client.get_or_create_collection.assert_called_once()

    def test_get_user_collection_uses_user_specific_name(self):
        from app.services import rag_service
        fake_client = MagicMock()
        fake_col = _fake_collection(name="user_42_personal")
        fake_client.get_or_create_collection.return_value = fake_col
        with patch.object(rag_service, "_chroma_client", return_value=fake_client):
            rag_service._get_user_collection(42)
        called_name = fake_client.get_or_create_collection.call_args.kwargs["name"]
        assert called_name == "user_42_personal"


# ─────────────────────────────────────────────────────────────────
# Indexing — index_user_document, index_cloud_document
# ─────────────────────────────────────────────────────────────────

class TestIndexUserDocument:
    def test_returns_zero_when_no_text_extracted(self, tmp_path):
        from app.services import rag_service
        f = tmp_path / "empty.txt"
        f.write_text("")
        with patch("app.utils.text_extraction.extract_text", return_value=""):
            n = rag_service.index_user_document(1, 42, str(f), "Empty")
        assert n == 0

    def test_returns_zero_when_chunking_yields_nothing(self, tmp_path):
        from app.services import rag_service
        f = tmp_path / "tiny.txt"
        f.write_text("hi")
        with patch("app.utils.text_extraction.extract_text", return_value="hi"):
            with patch("app.utils.text_extraction.chunk_text", return_value=[]):
                n = rag_service.index_user_document(1, 42, str(f), "Tiny")
        assert n == 0

    def test_success_upserts_chunks_with_correct_ids(self, tmp_path):
        from app.services import rag_service
        f = tmp_path / "doc.txt"
        f.write_text("body")
        chunks = ["chunk a", "chunk b", "chunk c"]
        col = _fake_collection()
        with patch("app.utils.text_extraction.extract_text", return_value="body"):
            with patch("app.utils.text_extraction.chunk_text", return_value=chunks):
                with patch.object(rag_service, "_contextual_embed",
                                  return_value=[[0.1] * 5] * 3):
                    with patch.object(rag_service, "_get_user_collection",
                                      return_value=col):
                        n = rag_service.index_user_document(99, 42, str(f), "Title")
        assert n == 3
        col.upsert.assert_called_once()
        kwargs = col.upsert.call_args.kwargs
        assert kwargs["ids"] == [
            "userdoc_99_chunk_0", "userdoc_99_chunk_1", "userdoc_99_chunk_2",
        ]
        # Metadata propagates user_id, doc_id, display_title
        assert kwargs["metadatas"][0]["user_id"] == 42
        assert kwargs["metadatas"][0]["display_title"] == "Title"

    def test_chroma_failure_returns_zero(self, tmp_path):
        from app.services import rag_service
        f = tmp_path / "doc.txt"
        f.write_text("body")
        col = _fake_collection()
        col.upsert.side_effect = RuntimeError("chroma down")
        with patch("app.utils.text_extraction.extract_text", return_value="body"):
            with patch("app.utils.text_extraction.chunk_text",
                       return_value=["c1", "c2"]):
                with patch.object(rag_service, "_contextual_embed",
                                  return_value=[[0.1] * 5] * 2):
                    with patch.object(rag_service, "_get_user_collection",
                                      return_value=col):
                        n = rag_service.index_user_document(1, 42, str(f), "X")
        assert n == 0


class TestIndexCloudDocument:
    def test_uses_clouddoc_id_prefix(self, tmp_path):
        from app.services import rag_service
        f = tmp_path / "x.txt"
        f.write_text("body")
        col = _fake_collection()
        with patch("app.utils.text_extraction.extract_text", return_value="body"):
            with patch("app.utils.text_extraction.chunk_text",
                       return_value=["c1", "c2"]):
                with patch.object(rag_service, "_contextual_embed",
                                  return_value=[[0.1] * 5] * 2):
                    with patch.object(rag_service, "_get_user_collection",
                                      return_value=col):
                        rag_service.index_cloud_document(7, 42, str(f), "Cloud Doc")
        ids = col.upsert.call_args.kwargs["ids"]
        assert ids[0].startswith("clouddoc_7_chunk_")


# ─────────────────────────────────────────────────────────────────
# Removal helpers
# ─────────────────────────────────────────────────────────────────

class TestRemoveFromIndex:
    def test_remove_user_document_skips_zero_chunks(self):
        from app.services import rag_service
        col = _fake_collection()
        with patch.object(rag_service, "_get_user_collection", return_value=col):
            rag_service.remove_user_document_from_index(42, 1, 0)
        col.delete.assert_not_called()

    def test_remove_user_document_deletes_all_chunk_ids(self):
        from app.services import rag_service
        col = _fake_collection()
        with patch.object(rag_service, "_get_user_collection", return_value=col):
            rag_service.remove_user_document_from_index(42, 99, 4)
        col.delete.assert_called_once()
        ids = col.delete.call_args.kwargs.get("ids") or col.delete.call_args.args[0]
        assert ids == [
            "userdoc_99_chunk_0", "userdoc_99_chunk_1",
            "userdoc_99_chunk_2", "userdoc_99_chunk_3",
        ]

    def test_remove_user_document_swallows_chroma_error(self):
        from app.services import rag_service
        col = _fake_collection()
        col.delete.side_effect = RuntimeError("chroma down")
        with patch.object(rag_service, "_get_user_collection", return_value=col):
            # Must NOT raise
            rag_service.remove_user_document_from_index(42, 99, 4)

    def test_remove_cloud_document_uses_clouddoc_prefix(self):
        from app.services import rag_service
        col = _fake_collection()
        with patch.object(rag_service, "_get_user_collection", return_value=col):
            rag_service.remove_cloud_document_from_index(42, 8, 2)
        ids = col.delete.call_args.kwargs.get("ids") or col.delete.call_args.args[0]
        assert ids == ["clouddoc_8_chunk_0", "clouddoc_8_chunk_1"]


class TestDeleteCollections:
    def test_delete_user_collection_calls_chromadb_delete(self):
        from app.services import rag_service
        client = MagicMock()
        with patch("chromadb.PersistentClient", return_value=client):
            rag_service.delete_user_collection(42)
        client.delete_collection.assert_called_once_with("user_42_personal")

    def test_delete_user_collection_swallows_errors(self):
        from app.services import rag_service
        client = MagicMock()
        client.delete_collection.side_effect = RuntimeError("not found")
        with patch("chromadb.PersistentClient", return_value=client):
            # No raise
            rag_service.delete_user_collection(42)


# ─────────────────────────────────────────────────────────────────
# get_user_index_summary
# ─────────────────────────────────────────────────────────────────

class TestGetUserIndexSummary:
    def test_dedupes_chunks_per_source(self):
        from app.services import rag_service

        # 3 user_document chunks for same doc + 1 cloud + 1 unknown source_type
        col = _fake_collection(get_result={
            "ids": [],
            "metadatas": [
                {"source_type": "user_document", "user_doc_id": "5",
                 "display_title": "User Doc"},
                {"source_type": "user_document", "user_doc_id": "5",
                 "display_title": "User Doc"},
                {"source_type": "user_document", "user_doc_id": "5",
                 "display_title": "User Doc"},
                {"source_type": "cloud_document", "cloud_doc_id": "9",
                 "display_title": "Cloud File"},
                {"source_type": "weird"},  # unknown → skipped
            ],
        })
        with patch.object(rag_service, "_get_user_collection", return_value=col):
            sources = rag_service.get_user_index_summary(42)
        # 2 unique sources
        assert len(sources) == 2
        by_id = {s["source_id"]: s for s in sources}
        assert by_id["5"]["chunk_count"] == 3
        assert by_id["9"]["chunk_count"] == 1

    def test_returns_empty_on_chroma_failure(self):
        from app.services import rag_service
        col = _fake_collection()
        col.get.side_effect = RuntimeError("chroma down")
        with patch.object(rag_service, "_get_user_collection", return_value=col):
            assert rag_service.get_user_index_summary(42) == []


# ─────────────────────────────────────────────────────────────────
# delete_user_indexed_source
# ─────────────────────────────────────────────────────────────────

class TestDeleteUserIndexedSource:
    def test_unknown_source_type_returns_zero(self):
        from app.services import rag_service
        assert rag_service.delete_user_indexed_source(42, "weird-type", "1") == 0

    def test_deletes_matching_chunks(self):
        from app.services import rag_service
        col = _fake_collection(get_result={
            "ids": ["userdoc_5_chunk_0", "userdoc_5_chunk_1", "userdoc_6_chunk_0"],
            "metadatas": [
                {"source_type": "user_document", "user_doc_id": 5},
                {"source_type": "user_document", "user_doc_id": 5},
                {"source_type": "user_document", "user_doc_id": 6},
            ],
        })
        with patch.object(rag_service, "_get_user_collection", return_value=col):
            n = rag_service.delete_user_indexed_source(42, "user_document", "5")
        assert n == 2
        # delete called with the 2 matching ids
        deleted = col.delete.call_args.kwargs.get("ids") or col.delete.call_args.args[0]
        assert set(deleted) == {"userdoc_5_chunk_0", "userdoc_5_chunk_1"}

    def test_no_matches_does_not_call_delete(self):
        from app.services import rag_service
        col = _fake_collection(get_result={"ids": [], "metadatas": []})
        with patch.object(rag_service, "_get_user_collection", return_value=col):
            n = rag_service.delete_user_indexed_source(42, "user_document", "999")
        assert n == 0
        col.delete.assert_not_called()

    def test_chroma_error_returns_negative_one(self):
        from app.services import rag_service
        col = _fake_collection()
        col.get.side_effect = RuntimeError("boom")
        with patch.object(rag_service, "_get_user_collection", return_value=col):
            n = rag_service.delete_user_indexed_source(42, "user_document", "1")
        assert n == -1


# ─────────────────────────────────────────────────────────────────
# get_index_stats / clear_index
# ─────────────────────────────────────────────────────────────────

class TestGetIndexStats:
    def test_returns_count_when_chroma_ok(self):
        from app.services import rag_service
        col = _fake_collection(count=42)
        with patch.object(rag_service, "_get_collection", return_value=col):
            stats = rag_service.get_index_stats()
        assert stats["indexed_sections"] == 42
        assert stats["status"] == "ok"

    def test_returns_error_status_on_chroma_failure(self):
        from app.services import rag_service
        with patch.object(rag_service, "_get_collection",
                          side_effect=RuntimeError("chroma offline")):
            stats = rag_service.get_index_stats()
        assert stats["indexed_sections"] == 0
        assert "erreur" in stats["status"].lower()


# ─────────────────────────────────────────────────────────────────
# index_raw_article
# ─────────────────────────────────────────────────────────────────

class TestIndexRawArticle:
    def test_empty_article_returns_false(self):
        from app.services import rag_service
        assert rag_service.index_raw_article({}) is False
        assert rag_service.index_raw_article({"titre": "", "summary": ""}) is False

    def test_success_upserts_one_doc(self):
        from app.services import rag_service
        col = _fake_collection()
        with patch.object(rag_service, "_embed", return_value=[[0.1] * 5]):
            with patch.object(rag_service, "_get_collection", return_value=col):
                ok = rag_service.index_raw_article({
                    "titre": "Article title",
                    "summary": "Summary body",
                    "lien": "https://example.com/article",
                    "source": "Source name",
                    "score": 7,
                    "category": "tech",
                    "theme": "ai",
                })
        assert ok is True
        col.upsert.assert_called_once()
        kwargs = col.upsert.call_args.kwargs
        assert len(kwargs["ids"]) == 1
        assert kwargs["metadatas"][0]["source_type"] == "staged_article"
        assert kwargs["metadatas"][0]["titre"] == "Article title"

    def test_chroma_failure_returns_false(self):
        from app.services import rag_service
        col = _fake_collection()
        col.upsert.side_effect = RuntimeError("chroma down")
        with patch.object(rag_service, "_embed", return_value=[[0.1] * 5]):
            with patch.object(rag_service, "_get_collection", return_value=col):
                ok = rag_service.index_raw_article({
                    "titre": "Title",
                    "summary": "Summary",
                })
        assert ok is False


# ─────────────────────────────────────────────────────────────────
# _query_collection — error handling
# ─────────────────────────────────────────────────────────────────

class TestQueryCollection:
    def test_empty_collection_returns_empty(self):
        from app.services import rag_service
        col = _fake_collection(count=0)
        d, m, dist = rag_service._query_collection(col, [0.1, 0.2], top_k=10)
        assert d == [] and m == [] and dist == []

    def test_chroma_error_returns_empty(self):
        from app.services import rag_service
        col = _fake_collection(count=10)
        col.query.side_effect = RuntimeError("chroma down")
        d, m, dist = rag_service._query_collection(col, [0.1], top_k=5)
        assert d == [] and m == [] and dist == []

    def test_normal_query_returns_first_row(self):
        from app.services import rag_service
        col = _fake_collection(count=5, query_result={
            "ids": [["a", "b"]],
            "documents": [["doc-a", "doc-b"]],
            "metadatas": [[{"k": "1"}, {"k": "2"}]],
            "distances": [[0.1, 0.2]],
        })
        d, m, dist = rag_service._query_collection(col, [0.1], top_k=5)
        assert d == ["doc-a", "doc-b"]
        assert dist == [0.1, 0.2]


# ─────────────────────────────────────────────────────────────────
# _build_source — dispatch by source_type
# ─────────────────────────────────────────────────────────────────

class TestBuildSource:
    def test_library_source(self):
        from app.services.rag_service import _build_source
        ref, src = _build_source(
            1, "doc body",
            {"source_type": "library", "display_title": "Manual",
             "filename": "manual.pdf", "tag": "guide"},
            dist=0.2,
        )
        assert "Manual" in ref
        assert src["source_type"] == "library"
        assert src["relevance"] == 80
        assert src["excerpt"].startswith("doc body")

    def test_user_document_source(self):
        from app.services.rag_service import _build_source
        ref, src = _build_source(
            2, "user doc",
            {"source_type": "user_document", "display_title": "Notes",
             "user_doc_id": 7},
            dist=0.5,
        )
        assert src["source_type"] == "user_document"
        assert src["user_doc_id"] == 7
        assert src["display_title"] == "Notes"

    def test_user_scraper_article_source(self):
        from app.services.rag_service import _build_source
        ref, src = _build_source(
            3, "scraped",
            {
                "source_type": "user_scraper_article",
                "source_name": "Hacker News",
                "article_title": "AI is here",
                "article_url": "https://news.example.com/post",
                "published_at": "2025-01-01",
            },
            dist=0.3,
        )
        assert src["source_type"] == "user_scraper_article"
        assert src["article_url"] == "https://news.example.com/post"
        assert "Hacker News" in ref

    def test_staged_article_source(self):
        from app.services.rag_service import _build_source
        ref, src = _build_source(
            4, "staged",
            {
                "source_type": "staged_article",
                "titre": "Headline",
                "source": "Le Monde",
                "lien": "https://lemonde.example.com/article",
                "category": "tech",
            },
            dist=0.4,
        )
        assert src["source_type"] == "staged_article"
        assert "Le Monde" in ref

    def test_newsletter_source_default(self):
        from app.services.rag_service import _build_source
        ref, src = _build_source(
            5, "newsletter content",
            {
                "section_title": "AI roundup",
                "newsletter_title": "Weekly",
                "newsletter_date": "2025-03-15",
                "newsletter_id": 42,
            },
            dist=0.1,
        )
        assert src["source_type"] == "newsletter"
        assert src["newsletter_id"] == 42
        assert "AI roundup" in ref


# ─────────────────────────────────────────────────────────────────
# _mmr_select — diversity selection
# ─────────────────────────────────────────────────────────────────

class TestMmrSelect:
    def test_returns_input_when_top_k_exceeds_size(self):
        from app.services.rag_service import _mmr_select
        rows = [("doc a", {}, 0.1, 0.5), ("doc b", {}, 0.2, 0.4)]
        assert _mmr_select(rows, top_k=10) == rows

    def test_picks_best_then_diversifies(self):
        from app.services.rag_service import _mmr_select
        rows = [
            ("apple banana cherry", {}, 0.1, 1.0),    # most relevant
            ("apple banana cherry orange", {}, 0.2, 0.9),  # near-duplicate
            ("kiwi mango papaya", {}, 0.3, 0.8),      # diverse
        ]
        out = _mmr_select(rows, top_k=2, lambda_=0.5)
        # First pick: highest RRF
        assert out[0][3] == 1.0
        # Second pick: the diverse one (low text overlap with first)
        assert out[1][0] == "kiwi mango papaya"


# ─────────────────────────────────────────────────────────────────
# _hyde_embed
# ─────────────────────────────────────────────────────────────────

class TestHydeEmbed:
    def test_combines_query_and_hypothetical_embeddings(self):
        from app.services import rag_service
        with patch.object(rag_service, "_llm_generate",
                          return_value="hypothetical document text"):
            with patch.object(rag_service, "_embed_query",
                              return_value=[1.0, 1.0, 1.0]):
                with patch.object(rag_service, "_embed",
                                  return_value=[[3.0, 3.0, 3.0]]):
                    out = rag_service._hyde_embed("question?")
        # Average: (1+3)/2 = 2.0 in each dimension
        assert out == [2.0, 2.0, 2.0]


# ─────────────────────────────────────────────────────────────────
# _generate_query_variants
# ─────────────────────────────────────────────────────────────────

class TestGenerateQueryVariants:
    def test_returns_original_plus_n_variants(self):
        from app.services import rag_service
        with patch.object(rag_service, "_llm_generate",
                          return_value="variant 1\nvariant 2\nvariant 3"):
            variants = rag_service._generate_query_variants("original query", n=2)
        assert variants[0] == "original query"
        assert len(variants) == 3  # 1 original + 2 variants

    def test_falls_back_to_query_on_llm_failure(self):
        from app.services import rag_service
        with patch.object(rag_service, "_llm_generate",
                          side_effect=Exception("LLM down")):
            variants = rag_service._generate_query_variants("query")
        assert variants == ["query"]


# ─────────────────────────────────────────────────────────────────
# _effective_top_k
# ─────────────────────────────────────────────────────────────────

class TestEffectiveTopK:
    def test_simple_query_uses_base_top_k(self):
        from app.services import rag_service
        result = rag_service._effective_top_k("Python language?")
        assert result == rag_service._TOP_K

    def test_long_synthesis_query_increases_top_k(self):
        from app.services import rag_service
        long_synth = (
            "compare and analyze the differences between approach A "
            "and approach B for solving distributed consensus problems"
        )
        result = rag_service._effective_top_k(long_synth)
        assert result == min(rag_service._TOP_K + 4, 12)
        assert result > rag_service._TOP_K


# ─────────────────────────────────────────────────────────────────
# _recency_factor
# ─────────────────────────────────────────────────────────────────

class TestRecencyFactor:
    def test_personal_doc_returns_1(self):
        from app.services.rag_service import _recency_factor
        assert _recency_factor({"source_type": "user_document"}) == 1.0
        assert _recency_factor({"source_type": "library"}) == 1.0

    def test_no_timestamp_returns_1(self):
        from app.services.rag_service import _recency_factor
        assert _recency_factor({"source_type": "user_scraper_article"}) == 1.0

    def test_recent_article_close_to_1(self):
        from app.services.rag_service import _recency_factor
        ts = datetime.now(timezone.utc).isoformat()
        f = _recency_factor({"source_type": "user_scraper_article", "published_at": ts})
        assert 0.9 < f <= 1.0

    def test_old_article_decayed(self):
        from app.services.rag_service import _recency_factor
        old = (datetime.now(timezone.utc) - timedelta(days=365 * 2)).isoformat()
        f = _recency_factor({"source_type": "user_scraper_article", "published_at": old})
        assert f < 0.5

    def test_invalid_timestamp_returns_1(self):
        from app.services.rag_service import _recency_factor
        f = _recency_factor({
            "source_type": "user_scraper_article", "published_at": "not-a-date",
        })
        assert f == 1.0


# ─────────────────────────────────────────────────────────────────
# _search_cache_key
# ─────────────────────────────────────────────────────────────────

class TestSearchCacheKey:
    def test_deterministic_for_same_inputs(self):
        from app.services.rag_service import _search_cache_key
        a = _search_cache_key("query", "shared", 1, None, "tag")
        b = _search_cache_key("query", "shared", 1, None, "tag")
        assert a == b

    def test_different_inputs_produce_different_keys(self):
        from app.services.rag_service import _search_cache_key
        a = _search_cache_key("query A", "shared", 1, None, None)
        b = _search_cache_key("query B", "shared", 1, None, None)
        assert a != b

    def test_returns_short_prefixed_string(self):
        from app.services.rag_service import _search_cache_key
        k = _search_cache_key("q", "shared", 1, None, None)
        assert k.startswith("rag:")


# ─────────────────────────────────────────────────────────────────
# _get_cache
# ─────────────────────────────────────────────────────────────────

class TestGetCache:
    def test_returns_none_when_ttl_zero(self, monkeypatch):
        from app.services import rag_service
        monkeypatch.setattr(rag_service, "_CACHE_TTL", 0)
        assert rag_service._get_cache() is None

    def test_returns_none_when_no_redis_url(self, monkeypatch):
        from app.services import rag_service
        monkeypatch.setattr(rag_service, "_CACHE_TTL", 60)
        monkeypatch.delenv("REDIS_URL", raising=False)
        assert rag_service._get_cache() is None


# ─────────────────────────────────────────────────────────────────
# _hybrid_query_collection — fusion of dense + sparse
# ─────────────────────────────────────────────────────────────────

class TestHybridQueryCollection:
    def test_empty_collection_returns_empty(self):
        from app.services import rag_service
        col = _fake_collection(count=0)
        d, m, dist, rrf = rag_service._hybrid_query_collection(
            col, [0.1, 0.2], "query text", n=5,
        )
        assert d == [] and m == [] and dist == [] and rrf == []

    def test_dense_only_when_no_keywords(self):
        from app.services import rag_service
        col = _fake_collection(count=10, query_result={
            "ids": [["a", "b"]],
            "documents": [["doc-a", "doc-b"]],
            "metadatas": [[{"x": 1}, {"x": 2}]],
            "distances": [[0.1, 0.2]],
        })
        # Mock _extract_keywords to return empty so only dense pass runs
        with patch.object(rag_service, "_extract_keywords", return_value=[]):
            d, m, dist, rrf = rag_service._hybrid_query_collection(
                col, [0.1, 0.2], "x", n=5,
            )
        assert d == ["doc-a", "doc-b"]
        # RRF scores assigned
        assert all(s > 0 for s in rrf)
