"""Coverage for the small RAG hygiene fixes from audit round 4
(A3-M2 empty-input guard, A3-L1 NFC cache-key normalisation, A3-L3
DEBUG logs scrubbed of raw user content).
"""
from unittest.mock import MagicMock, patch


# ── A3-M2: blank inputs don't reach the provider ─────────────────────────────

def test_embed_empty_list_returns_empty():
    from app.services.rag_service import _embed
    assert _embed([]) == []


def test_embed_blank_strings_yield_zero_vectors_in_order():
    """Three inputs, middle one blank → output still has 3 vectors, with
    the middle slot a zero vector and the outer two real embeddings."""
    from app.services import rag_service
    from app.services.rag_service import _embed
    real_vec = [0.1, 0.2, 0.3]
    with patch.object(rag_service, "_use_mistral_embed", return_value=False), \
         patch.object(rag_service, "_embed_ollama_batch", return_value=[real_vec, real_vec]):
        out = _embed(["one", "  ", "three"])
    assert len(out) == 3
    assert out[0] == real_vec
    assert all(v == 0.0 for v in out[1])  # zero-vector placeholder
    assert out[2] == real_vec


def test_embed_all_blank_inputs_no_http_call():
    """If every input is blank, the provider must NEVER be called and a
    zero-vector list of the right shape is returned."""
    from app.services import rag_service
    from app.services.rag_service import _embed
    with patch.object(rag_service, "_use_mistral_embed", return_value=False), \
         patch.object(rag_service, "_embed_ollama_batch") as ollama, \
         patch.object(rag_service, "_active_embed_dim", return_value=1024):
        out = _embed(["", "  ", None])
    assert len(out) == 3
    assert all(v == 0.0 for v in out[0])
    ollama.assert_not_called()


# ── A3-L1: NFC normalisation on the cache key ────────────────────────────────

def test_embed_cache_key_nfc_collapses_combining_forms():
    """Pre-composed é (U+00E9) and decomposed e + ́ (U+0065 U+0301) hash
    to the same cache key after NFC normalisation."""
    from app.services.rag_service import _embed_cache_key
    a = _embed_cache_key("é")           # pre-composed
    b = _embed_cache_key("é")     # decomposed
    assert a == b


def test_embed_cache_key_distinct_for_different_text():
    """Sanity: different content still produces different keys."""
    from app.services.rag_service import _embed_cache_key
    assert _embed_cache_key("alpha") != _embed_cache_key("beta")


# ── A3-L3: DEBUG logs no longer echo raw user content ────────────────────────

def test_hyde_debug_log_emits_length_not_content():
    """The HyDE happy path must log a length + hash prefix, never the
    hypothetical passage itself."""
    from app.services import rag_service
    from app.services.rag_service import _hyde_embed

    # Force both branches of the inner pool: _embed_query returns a fixed
    # vector, _llm_generate returns a fake hypothetical, _embed embeds it.
    secret_passage = "SECRET-PII-DO-NOT-LOG"
    plain = [0.1] * 4
    with patch.object(rag_service, "_embed_query", return_value=plain), \
         patch.object(rag_service, "_llm_generate", return_value=secret_passage), \
         patch.object(rag_service, "_embed", return_value=[plain]), \
         patch.object(rag_service, "_get_cache", return_value=None), \
         patch.object(rag_service.logger, "debug") as dbg:
        _hyde_embed("what is the meaning of life")

    # Confirm the marker log fired but the passage is NOT in any of the
    # rendered messages (i.e. across all DEBUG calls).
    rendered = []
    for call in dbg.call_args_list:
        msg = call.args[0] if call.args else ""
        try:
            rendered.append(msg % call.args[1:])
        except Exception:
            rendered.append(msg)
    joined = " ".join(rendered)
    assert "HyDE hypothetical generated" in joined
    assert secret_passage not in joined


def test_reranker_debug_log_does_not_echo_query():
    """The reranker DEBUG log keeps the query length, never the query text."""
    from app.services import rag_service

    # Build a minimal scenario where the reranker fires through the
    # debug branch. We call _rerank() directly with mocked LLM.
    rows = [
        (f"doc-{i}", {}, 0.5, 0.5)
        for i in range(4)
    ]
    secret_query = "PRIVATE-QUERY-content"
    with patch.object(rag_service, "_llm_generate", return_value="1. 2. 3. 4."), \
         patch.object(rag_service.logger, "debug") as dbg:
        try:
            rag_service._rerank(secret_query, rows, top_k=2)
        except Exception:
            # We don't care about the rerank output shape here — only the log.
            pass

    rendered = " ".join(
        (call.args[0] % call.args[1:]) if len(call.args) > 1 else str(call.args[0] if call.args else "")
        for call in dbg.call_args_list
    )
    assert secret_query not in rendered
