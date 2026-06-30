"""Coverage for the dim-coherence guard on reindex (audit round 4, A3-C3).

A crash mid-reindex used to leave a collection with vectors from two
different embed spaces (e.g. 1024-dim Mistral half, 1536-dim OpenAI
half). The next `col.update(...)` would either fail or — worse —
succeed for the new docs and leave the old ones unreachable. The
pre-flight assertion now refuses to write into a collection whose
first vector's dim differs from what the active model would produce.
"""
from unittest.mock import MagicMock, patch


def test_active_embed_dim_static_lookup_for_known_byok_model():
    """When the (provider, model) pair is in _BYOK_EMBED_DIM, the helper
    returns the static value without calling the network."""
    from app.services import rag_service

    rag_service._reset_active_embed_dim_cache()
    with patch.object(rag_service, "_active_embed_provider", return_value="voyage"), \
         patch.object(rag_service, "_active_embed_model", return_value="voyage-3"), \
         patch.object(rag_service, "_embed_query") as probe:
        dim = rag_service._active_embed_dim()
    assert dim == 1024
    probe.assert_not_called()  # static table win, no network


def test_active_embed_dim_probes_when_unknown():
    """Unknown model → probe via _embed_query, cache the result."""
    from app.services import rag_service

    rag_service._reset_active_embed_dim_cache()
    with patch.object(rag_service, "_active_embed_provider", return_value="ollama"), \
         patch.object(rag_service, "_active_embed_model", return_value="custom-bge"), \
         patch.object(rag_service, "_embed_query", return_value=[0.1] * 768) as probe:
        dim = rag_service._active_embed_dim()
    assert dim == 768
    probe.assert_called_once()


def test_collection_first_vector_dim_empty_returns_none():
    from app.services.rag_service import _collection_first_vector_dim
    col = MagicMock()
    col.count.return_value = 0
    assert _collection_first_vector_dim(col) is None
    col.peek.assert_not_called()


def test_collection_first_vector_dim_populated_returns_len():
    from app.services.rag_service import _collection_first_vector_dim
    col = MagicMock()
    col.count.return_value = 5
    col.peek.return_value = {"embeddings": [[0.0] * 1536]}
    assert _collection_first_vector_dim(col) == 1536


def test_assert_collection_compat_passes_on_empty_collection():
    from app.services import rag_service
    from app.services.rag_service import assert_collection_compat_for_reindex
    col = MagicMock()
    col.count.return_value = 0
    rag_service._reset_active_embed_dim_cache()
    with patch.object(rag_service, "_active_embed_dim", return_value=1024):
        assert_collection_compat_for_reindex(col)  # no exception


def test_assert_collection_compat_passes_on_matching_dim():
    from app.services import rag_service
    from app.services.rag_service import assert_collection_compat_for_reindex
    col = MagicMock()
    col.count.return_value = 10
    col.peek.return_value = {"embeddings": [[0.0] * 1024]}
    col.metadata = {"embed_model": "mistral-embed"}
    rag_service._reset_active_embed_dim_cache()
    with patch.object(rag_service, "_active_embed_dim", return_value=1024):
        assert_collection_compat_for_reindex(col)


def test_assert_collection_compat_raises_on_dim_mismatch():
    """The canonical "crash mid-reindex" footprint: existing dim 1024
    while the active model now emits 1536 — must raise."""
    import pytest

    from app.services import rag_service
    from app.services.rag_service import EmbedDimensionMismatch, assert_collection_compat_for_reindex
    col = MagicMock()
    col.count.return_value = 5
    col.peek.return_value = {"embeddings": [[0.0] * 1024]}
    col.metadata = {"embed_model": "mistral-embed"}
    rag_service._reset_active_embed_dim_cache()
    with patch.object(rag_service, "_active_embed_dim", return_value=1536):
        with pytest.raises(EmbedDimensionMismatch) as exc:
            assert_collection_compat_for_reindex(col)
    assert exc.value.existing["dim"] == 1024
    assert exc.value.requested["dim"] == 1536


def test_assert_collection_compat_skips_when_active_dim_unknown():
    """If we can't probe the active model (network down, weird provider),
    the assertion is best-effort and lets the reindex run."""
    from app.services import rag_service
    from app.services.rag_service import assert_collection_compat_for_reindex
    col = MagicMock()
    col.count.return_value = 5
    col.peek.return_value = {"embeddings": [[0.0] * 1024]}
    rag_service._reset_active_embed_dim_cache()
    with patch.object(rag_service, "_active_embed_dim", return_value=None):
        assert_collection_compat_for_reindex(col)  # no exception


# Integration coverage of the CLI dim-mismatch path is exercised in
# tests/test_cli_reindex_lock.py; the unit-level pre-flight contract is
# what's hard to get right and is fully covered above.
