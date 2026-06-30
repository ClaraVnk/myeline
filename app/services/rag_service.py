import functools
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import requests
from bs4 import BeautifulSoup

from app.utils.logger import get_logger


logger = get_logger("rag_service", subfolder="services")


def _build_http_session() -> requests.Session:
    """Module-level HTTP client with keep-alive + connection pooling.

    A typical RAG query fires 5-10 HTTP requests at the LLM stack (HyDE
    embed, multi-query embed, rerank, optional contextual retrieval, final
    synthesis). With a fresh ``requests.post`` per call each one pays a
    TCP + TLS handshake — 50-300 ms of unrelated wall-clock time per
    request, eaten by the user's spinner. A pooled Session reuses the
    connection so we pay the handshake once per remote per worker
    lifetime.

    Pool sizing: 20 simultaneous connections per host comfortably covers
    parallel variant embedding (max 4) plus the worker concurrency we
    target (RQ + queue concurrency). ``pool_block=False`` means the
    Session never blocks the caller — it just opens a fresh connection if
    the pool is exhausted, matching the previous unpooled behaviour as a
    safety net.
    """
    s = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=20,
        pool_maxsize=20,
        pool_block=False,
        max_retries=0,  # retry/backoff is handled by _mistral_post / _gemini_post
    )
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


_HTTP = _build_http_session()


_CHROMA_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "chroma_db"
_COLLECTION_NAME = "newsletter_sections"
_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")
_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "bge-m3")
_LLM_MODEL = os.getenv("OLLAMA_LLM_MODEL", "qwen2.5:1.5b")
_TOP_K = 8
# Candidates retrieved per sub-collection before the final reranking step.
# Must be > _TOP_K so the reranker has room to work.
_RETRIEVAL_K = int(os.getenv("RAG_RETRIEVAL_K", "30"))
# Ollama model used for reranking. mistral-nemo (7.1B) outperforms qwen2.5:1.5b for
# passage-level relevance judgments; falls back to the synthesis model if not set.
_RERANK_MODEL = os.getenv("OLLAMA_RERANK_MODEL", "mistral-nemo")
# Set to "false" to disable LLM reranking (saves one Ollama call; RRF+MMR still applies).
_RERANK_ENABLED = os.getenv("RAG_RERANK_ENABLED", "true").lower() not in ("false", "0", "no")
# Result cache in Redis. TTL in seconds; set to 0 to disable.
_CACHE_TTL = int(os.getenv("RAG_CACHE_TTL", "120"))
# Exponential recency decay for article/scraper content (newsletters & personal docs unaffected).
# Enabled by default: RSS/scraper articles are time-sensitive; fresh content should rank higher.
_RECENCY_BOOST = os.getenv("RAG_RECENCY_BOOST", "true").lower() not in ("false", "0", "no")
_RECENCY_HALF_LIFE_DAYS = int(os.getenv("RAG_RECENCY_HALF_LIFE_DAYS", "180"))
# Global Mistral API key — when set, all LLM calls (HyDE, reranker, query variants,
# synthesis) route to Mistral instead of Ollama. Embedding stays local (bge-m3).
# Mistral is the platform default (French/EU-hosted, GDPR-aligned).
# Synthesis uses _MISTRAL_LLM_MODEL (medium); auxiliary calls (HyDE, rerank, query
# variants, contextual retrieval) use _MISTRAL_AUX_MODEL (small) to control cost.
_MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
_MISTRAL_LLM_MODEL = os.getenv("MISTRAL_LLM_MODEL", "mistral-medium-latest")
_MISTRAL_AUX_MODEL = os.getenv("MISTRAL_AUX_MODEL", "mistral-small-latest")
_MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"

# Embedding provider — "ollama" (local bge-m3) or "mistral" (API). Default
# stays "ollama" so Sovereign / Sovereign-hybrid / local dev keep working
# without env changes. SaaS prod sets EMBED_PROVIDER=mistral.
# Mistral embed-v2 returns 1024-dim vectors, same as bge-m3 → no schema
# change on ChromaDB. But vector spaces differ across providers, so any
# corpus indexed under one provider must be re-embedded before serving
# queries under the other (see `flask reindex-embeddings`).
_EMBED_PROVIDER = os.getenv("EMBED_PROVIDER", "ollama").lower()
_MISTRAL_EMBED_MODEL = os.getenv("MISTRAL_EMBED_MODEL", "mistral-embed")
_MISTRAL_EMBED_API_URL = "https://api.mistral.ai/v1/embeddings"
# Mistral accepts up to 16k tokens per batch; capping at 256 inputs gives
# a comfortable safety margin and avoids hitting the body-size ceiling.
_MISTRAL_EMBED_BATCH_SIZE = 256

# ── User BYOK embedding ───────────────────────────────────────────────────────
# Allowed providers for the per-user BYOK embedding (User.embed_provider).
# Each ships its own embedding model with a known vector dimension. The dim is
# used to refuse switches against a collection that already holds vectors of a
# different shape — Chroma would error on insert, but we'd rather fail upstream
# with a clear French message.
_BYOK_EMBED_PROVIDERS = ("mistral", "openai", "voyage", "cohere", "gemini")
_BYOK_EMBED_DEFAULT_MODEL = {
    "mistral": "mistral-embed",
    "openai": "text-embedding-3-small",
    "voyage": "voyage-3",
    "cohere": "embed-multilingual-v3.0",
    "gemini": "text-embedding-004",
}
# (provider, model) -> dimension. Used for upfront compatibility checks against
# existing ChromaDB collection metadata. Models not listed here are accepted
# but skip the dim guard (the collection's first insert will raise if mismatch).
_BYOK_EMBED_DIM = {
    ("mistral", "mistral-embed"): 1024,
    ("openai", "text-embedding-3-small"): 1536,
    ("openai", "text-embedding-3-large"): 3072,
    ("voyage", "voyage-3"): 1024,
    ("voyage", "voyage-3-lite"): 512,
    ("cohere", "embed-multilingual-v3.0"): 1024,
    ("cohere", "embed-multilingual-light-v3.0"): 384,
    ("gemini", "text-embedding-004"): 768,
}
# Batch size per provider (max inputs per single embeddings request).
_BYOK_EMBED_BATCH_SIZE = {
    "mistral": _MISTRAL_EMBED_BATCH_SIZE,
    "openai": 256,
    "voyage": 128,    # Voyage caps each request at ~120 inputs
    "cohere": 96,     # Cohere recommends ≤ 96 per call for embed-multilingual-v3
    "gemini": 100,    # Gemini batchEmbedContents caps at 100 requests per call
}


class EmbedDimensionMismatch(Exception):
    """Raised when a reindex would collide with the existing ChromaDB
    collection (different provider / model / dimension).

    The operator must wipe the collection and re-index before switching.
    Carries the resolved (provider, model, dim) we'd write today and the
    (provider, model, dim) the collection was built with so the UI can show
    both.
    """

    def __init__(self, collection_id: int, existing: dict, requested: dict):
        self.collection_id = collection_id
        self.existing = existing
        self.requested = requested
        super().__init__(
            f"Embed switch refused: collection built with "
            f"{existing} vs requested {requested}"
        )


def _use_mistral_embed() -> bool:
    """Return True if embeddings should route to Mistral API, False = local Ollama.

    Sovereign mode forces local-only inference regardless of EMBED_PROVIDER —
    the on-prem promise of "no external API call" overrides any config.
    """
    from app.utils.deployment_mode import is_sovereign
    if is_sovereign():
        return False
    return _EMBED_PROVIDER == "mistral" and bool(_MISTRAL_API_KEY)


def _active_embed_model(user_id: Optional[int] = None) -> str:
    """Return the name of the embedding model currently in use.

    Used as a cache-key prefix and ChromaDB collection metadata tag so
    queries are never served against vectors from a different model.
    On the Ollama path, honours the Redis admin override
    (_LOCAL_EMBED_MODEL_KEY) — lets a Sovereign admin swap embedder
    without restarting the app.

    When a per-user BYOK embedding is configured, returns that chosen embed
    model instead of the global default — so the user_{id}_personal
    collection is tagged consistently.
    """
    byok = _resolve_embed_byok(user_id)
    if byok is not None:
        return byok["model"]
    return _MISTRAL_EMBED_MODEL if _use_mistral_embed() else _resolved_local_embed_model()


def _active_embed_provider(user_id: Optional[int] = None) -> str:
    """Return the provider id currently in use ("ollama" | "mistral" | BYOK provider)."""
    byok = _resolve_embed_byok(user_id)
    if byok is not None:
        return byok["provider"]
    return "mistral" if _use_mistral_embed() else "ollama"


# Per-tier per-document token caps. Counted at ~4 chars/token (Mistral
# embed + bge-m3 average on FR/EN mix). Refuses absurdly large docs that
# would individually consume the user's monthly Mistral embed budget
# (e.g. a 1 GB PDF = ~200 M tokens = ~18 €, blowing through a Standard's
# 9.90 € revenue in a single upload). Sized to comfortably accommodate
# normal usage: a 200-page PDF runs ~100 k tokens, a thesis ~300 k.
# None = no cap (Enterprise). Caps are deliberately generous because the
# bigger backstop is the per-plan document count (Free = 500), so even at
# the per-doc cap a Free user's full corpus stays bounded.
# Pivot Free/Pro/Enterprise: gratuit (Free, BYOK) / premium_pro (Pro) /
# enterprise. Free still gets a sane per-doc cap to bound embed cost.
_EMBED_DOC_TOKEN_CAPS = {
    "gratuit": 150_000,
    "premium_pro": 2_000_000,
    "enterprise": None,
}


def _approx_tokens(text: str) -> int:
    """Cheap token estimate — accurate enough for budget enforcement."""
    return len(text) // 4 if text else 0


def check_doc_token_budget(text: str, abonnement_type: Optional[str]) -> Optional[str]:
    """Return a user-facing error message if the doc exceeds the tier cap, else None.

    Called from every indexing entry point (user_doc, cloud_doc, org_doc,
    scrapers) BEFORE chunking + _embed() so a giant doc is rejected cheap
    rather than after we've burned tokens and ChromaDB writes.
    """
    plan = (abonnement_type or "").lower()
    cap = _EMBED_DOC_TOKEN_CAPS.get(plan, _EMBED_DOC_TOKEN_CAPS["gratuit"])
    if cap is None:
        return None
    tokens = _approx_tokens(text)
    if tokens > cap:
        return (
            f"Document trop volumineux pour votre formule : "
            f"~{tokens // 1000} k tokens (plafond {cap // 1000} k). "
            "Découpez le document, ou passez à une formule supérieure pour "
            "indexer des fichiers plus grands."
        )
    return None

# Sovereign mode — when true, NO external AI provider is ever called (Mistral,
# OpenAI, Gemini, Claude all bypassed). Used for on-premise / regulated
# deployments. Resolved through app.utils.deployment_mode.is_sovereign() so
# DEPLOYMENT_MODE=sovereign and the legacy SOVEREIGN_MODE flag both work.

# Redis key used by the admin toggle to force Ollama even when a Mistral key is set.
_LLM_PROVIDER_KEY = "myeline:llm_provider"  # value: "mistral" | "ollama"
# Redis key holding the override for the local Ollama synthesis model. When set,
# replaces _LLM_MODEL for _chat_ollama() — lets the on-premise admin pick the
# active model from the list of pulled Ollama models without restarting the app.
_LOCAL_MODEL_KEY = "myeline:local_llm_model"
# Same pattern for the embedder. Switching it requires reindexing the corpus
# (vectors from the previous model are not comparable), so the admin endpoint
# that writes this key also enqueues a reindex RQ job and gates concurrent
# switches via _EMBED_REINDEX_LOCK_KEY.
_LOCAL_EMBED_MODEL_KEY = "myeline:local_embed_model"
_EMBED_REINDEX_LOCK_KEY = "myeline:embed_reindex_lock"  # set while a reindex job is in-flight


def _resolved_local_model() -> str:
    """Return the Ollama model used for synthesis: Redis override if set, else env."""
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        try:
            from redis import Redis
            val = Redis.from_url(redis_url, socket_connect_timeout=1).get(_LOCAL_MODEL_KEY)
            if val:
                return val.decode()
        except Exception:
            pass
    return _LLM_MODEL


def _resolved_local_embed_model() -> str:
    """Return the Ollama embedding model in use: Redis admin override > env > default.

    Only consulted when the Ollama embed path is active (cf. _use_mistral_embed).
    Sovereign deployments use this picker exclusively since they never route to
    the Mistral embed API.
    """
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        try:
            from redis import Redis
            val = Redis.from_url(redis_url, socket_connect_timeout=1).get(_LOCAL_EMBED_MODEL_KEY)
            if val:
                return val.decode()
        except Exception:
            pass
    return _EMBED_MODEL


def _use_mistral_api() -> bool:
    """Return True if Mistral should be used for LLM calls (synthesis + auxiliaries).

    Sovereign mode forces False unconditionally. Otherwise, the admin toggle can
    override the default (use Mistral when key is set) at runtime without a
    restart, storing the choice in Redis.
    """
    from app.utils.deployment_mode import is_sovereign
    if is_sovereign():
        return False
    if not _MISTRAL_API_KEY:
        return False
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        return True
    try:
        from redis import Redis
        val = Redis.from_url(redis_url, socket_connect_timeout=1).get(_LLM_PROVIDER_KEY)
        if val == b"ollama":
            return False
    except Exception:
        pass
    return True

# Contextual retrieval (Anthropic technique): generate a 1-sentence LLM summary of
# each chunk's role in the document and prepend it before embedding.
# Reduces retrieval failure rate by ~49 % on structured documents.
# Enabled by default when a Mistral key is present; costs N Mistral calls per document.
_CONTEXTUAL_EMBED_ENABLED = os.getenv(
    "RAG_CONTEXTUAL_EMBED",
    "true" if os.getenv("MISTRAL_API_KEY") else "false",
).lower() not in ("false", "0", "no")

# Multi-query retrieval: generate N query reformulations and merge results via RRF.
# Enabled by default when Mistral is available (handles variant generation).
_MULTIQUERY_ENABLED = os.getenv(
    "RAG_MULTIQUERY_ENABLED",
    "true" if os.getenv("MISTRAL_API_KEY") else "false",
).lower() in ("true", "1", "yes")
_MULTIQUERY_N = int(os.getenv("RAG_MULTIQUERY_N", "2"))

# HNSW index quality parameters.
# hnsw:M controls graph connectivity (more links = better recall, more memory).
# hnsw:search_ef controls the candidate list size at query time; ChromaDB default
# is 10 which is very low — 200 gives dramatically better recall with negligible
# latency overhead on collections of typical size (< 500k vectors).
_HNSW_M = int(os.getenv("CHROMA_HNSW_M", "32"))
_HNSW_SEARCH_EF = int(os.getenv("CHROMA_HNSW_SEARCH_EF", "200"))

# Track which collection names have already had their hnsw:search_ef updated this
# process lifetime so we don't issue a modify() call on every request.
_hnsw_updated: set[str] = set()


def _doc_date_meta(value) -> dict:
    """Convert a datetime / date / ISO string into Chroma-friendly date metadata.

    Returns a dict with two keys to inject in chunk metadata:
      - doc_date (str): YYYY-MM-DD, surfaced to the UI for date badges
      - doc_ts   (int): unix epoch seconds, used for $gte/$lte filtering
                        in Chroma `where` clauses (numeric comparison only).

    An empty value yields {} so callers can `**_doc_date_meta(...)` safely.
    """
    if not value:
        return {}
    try:
        if isinstance(value, datetime):
            dt = value
        elif hasattr(value, "year") and hasattr(value, "month"):
            # bare date — promote to UTC midnight
            dt = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
        else:
            s = str(value).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return {"doc_date": dt.date().isoformat(), "doc_ts": int(dt.timestamp())}
    except Exception:
        return {}

_RAG_SYSTEM_PROMPT_STRICT = """You are a strict document research assistant. The user queries their own document base.

ABSOLUTE RULES — any violation makes the response invalid:

1. EXCLUSIVE SOURCES: every factual claim comes from a numbered excerpt [N] provided below. Your general knowledge never completes, enriches, or contextualises answers — not even partially.

2. MISSING INFORMATION: if the answer is not in the excerpts, reply only:
   FR → "Je ne trouve pas cette information dans vos documents."
   EN → "I cannot find this information in your documents."
   Never infer, guess, or extrapolate from partial information.

3. PARTIAL INFORMATION: if an excerpt touches the topic without answering precisely, say so explicitly:
   "The documents mention [X] but do not specify [Y]."

4. MANDATORY CITATIONS: every factual statement carries a [N] referencing a real excerpt. Never invent a reference. At the end of your response, list:
   References: [N] Title — Source. Year. (URL if available)

5. LANGUAGE: respond in the same language as the user's question. French question → French answer. English question → English answer.

6. FORMAT — adapt to the request type:
   • Short factual question → 1–3 sentences
   • Synthesis / comparison → ## headings, - bullet lists, clear structure
   • Enumeration → numbered or bulleted list
   Do not repeat the question in your response."""


# Relaxed mode: lets the model add general-knowledge context when the
# excerpts don't fully answer, but only with explicit attribution. Used by
# users who want a usable answer rather than a flat refusal — typical for
# definitional or "what is X" queries.
_RAG_SYSTEM_PROMPT_RELAXED = """You are a research assistant. The user queries their own document base, with permission to draw on general knowledge when the excerpts fall short.

RULES:

1. SOURCES FIRST: prefer the numbered excerpts [N] for every factual claim. When an excerpt answers the question, cite it.

2. EXPLICIT BACKFILL: if the excerpts are silent or only partially answer, you MAY add general-knowledge context — but only when prefixed by an explicit caveat:
   FR → "Hors documents (connaissance générale) : …"
   EN → "Outside your documents (general knowledge): …"
   Never present general knowledge as if it came from the excerpts. Never put a [N] citation on a sentence that did not come from one.

3. CONFLICTS: if an excerpt and general knowledge conflict, surface the conflict and trust the excerpt. Tell the user.

4. NO FABRICATED CITATIONS: never invent a reference. The [N] markers must point to real excerpts.

5. LANGUAGE: respond in the same language as the user's question.

6. FORMAT — adapt to the request type:
   • Short factual question → 1–3 sentences
   • Synthesis / comparison → ## headings, - bullet lists, clear structure
   • Enumeration → numbered or bulleted list
   Do not repeat the question in your response."""


# Backwards-compatibility alias — older code paths import _RAG_SYSTEM_PROMPT.
_RAG_SYSTEM_PROMPT = _RAG_SYSTEM_PROMPT_STRICT


def _strip_html(html: str) -> str:
    return BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)


_SYNTHESIS_KEYWORDS = frozenset({
    "résume", "résumé", "résumer", "synthèse", "synthétise", "synthétiser",
    "compare", "comparer", "différence", "différences", "similitude",
    "analyse", "analyser", "explique", "expliquer", "décris", "décrire",
    "liste", "lister", "énumère", "énumérer", "quels", "quelles",
    "summarize", "difference", "differences", "analyze",
    "explain", "describe", "list", "what are", "how does", "overview",
})


def _max_tokens(query: str) -> int:
    """Return the synthesis token budget based on query complexity.

    Simple factual queries get 1024 tokens (~750 words).
    Synthesis / comparison / enumeration queries get 2048 (~1500 words).
    """
    words = query.lower().split()
    if len(words) > 12 or any(w in _SYNTHESIS_KEYWORDS for w in words):
        return 2048
    return 1024


# Gemini safety thresholds — kept for the per-org Gemini provider option only.
# The default platform LLM is Mistral, which has no equivalent safety toggle.
_GEMINI_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT",         "threshold": "BLOCK_ONLY_HIGH"},
    {"category": "HARM_CATEGORY_HATE_SPEECH",        "threshold": "BLOCK_ONLY_HIGH"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",  "threshold": "BLOCK_ONLY_HIGH"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT",  "threshold": "BLOCK_ONLY_HIGH"},
]


class _MistralRateLimitError(Exception):
    """Raised when Mistral returns HTTP 429 after all retries are exhausted."""


class _GeminiRateLimitError(Exception):
    """Raised when Gemini returns HTTP 429 after all retries are exhausted (per-org provider)."""


def _mistral_post(payload: dict, api_key: str, timeout: int, max_retries: int = 3) -> requests.Response:
    """POST to Mistral chat/completions with automatic exponential-backoff retry on 429."""
    delay = 2.0
    for attempt in range(max_retries):
        resp = _HTTP.post(
            _MISTRAL_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        if resp.status_code != 429:
            return resp
        if attempt < max_retries - 1:
            logger.warning("Mistral 429 — retrying in %.1fs (attempt %d/%d)", delay, attempt + 1, max_retries)
            time.sleep(delay)
            delay *= 2
    raise _MistralRateLimitError(f"Mistral rate limit after {max_retries} retries")


def _gemini_post(url: str, params: dict, payload: dict, timeout: int, max_retries: int = 3) -> requests.Response:
    """POST to Gemini with automatic exponential-backoff retry on 429 (per-org provider only)."""
    delay = 2.0
    for attempt in range(max_retries):
        resp = _HTTP.post(url, params=params, headers={"Content-Type": "application/json"},
                             json=payload, timeout=timeout)
        if resp.status_code != 429:
            return resp
        if attempt < max_retries - 1:
            logger.warning("Gemini 429 — retrying in %.1fs (attempt %d/%d)", delay, attempt + 1, max_retries)
            time.sleep(delay)
            delay *= 2
    raise _GeminiRateLimitError(f"Gemini rate limit after {max_retries} retries")


def _hnsw_meta(extra: Optional[dict] = None) -> dict:
    """Base ChromaDB collection metadata with tuned HNSW parameters.

    `embed_model` and `embed_provider` are the *active* values (resolved through
    `_active_embed_model()` / `_active_embed_provider()`), not the static env —
    so a collection created after switching EMBED_PROVIDER reflects the provider
    that will actually serve queries against it.
    """
    m = {
        "hnsw:space": "cosine",
        "embed_model": _active_embed_model(),
        "embed_provider": _active_embed_provider(),
        "hnsw:M": _HNSW_M,
        "hnsw:construction_ef": 200,
        "hnsw:search_ef": _HNSW_SEARCH_EF,
    }
    if extra:
        m.update(extra)
    return m


def _ensure_search_ef(collection) -> None:
    """Update hnsw:search_ef on existing collections once per process lifetime.

    hnsw:M and hnsw:construction_ef cannot change without a full rebuild, but
    hnsw:search_ef only affects query-time candidate expansion and can be set
    at any time via collection.modify().
    """
    name = collection.name
    if name in _hnsw_updated:
        return
    if collection.metadata.get("hnsw:search_ef") != _HNSW_SEARCH_EF:
        try:
            new_meta = dict(collection.metadata)
            new_meta["hnsw:search_ef"] = _HNSW_SEARCH_EF
            collection.modify(metadata=new_meta)
            logger.debug("Updated hnsw:search_ef=%d for collection %s", _HNSW_SEARCH_EF, name)
        except Exception as exc:
            logger.debug("hnsw:search_ef update skipped for %s: %s", name, exc)
    _hnsw_updated.add(name)


_CHROMA_CLIENT = None


def _chroma_client():
    """Process-wide ChromaDB client.

    Each PersistentClient() call opens a SQLite + WAL handshake — fine
    once, but the codebase used to re-open one per `_get_*_collection`
    call, i.e. up to 3× per RAG query (perf audit M14). Caching the
    client at module scope cuts 50-200 ms off every search.
    """
    global _CHROMA_CLIENT
    if _CHROMA_CLIENT is None:
        import chromadb
        _CHROMA_PATH.mkdir(parents=True, exist_ok=True)
        _CHROMA_CLIENT = chromadb.PersistentClient(path=str(_CHROMA_PATH))
    return _CHROMA_CLIENT


def _get_collection():
    client = _chroma_client()
    col = client.get_or_create_collection(
        name=_COLLECTION_NAME,
        metadata=_hnsw_meta(),
    )
    _ensure_search_ef(col)
    return col


def _nomic_prefix(text: str, task: str) -> str:
    """Apply nomic-embed-text asymmetric task prefix only when using that model family."""
    if "nomic" in _EMBED_MODEL.lower():
        return f"{task}: {text}"
    return text


_EMBED_BATCH_SIZE = 32  # max chunks per Ollama embed request


def _embed_ollama_batch(inputs: list[str]) -> list[list[float]]:
    """Call Ollama /api/embed once with a batch of inputs.

    Uses the resolved local embed model (Redis override wins over env), so
    a Sovereign admin who switched the embedder via /admin sees the change
    take effect on the next request without restart.
    """
    resp = _HTTP.post(
        f"{_OLLAMA_URL}/api/embed",
        json={"model": _resolved_local_embed_model(), "input": inputs},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["embeddings"]


def _mistral_embed_post(inputs: list[str], max_retries: int = 3) -> list[list[float]]:
    """Call Mistral /v1/embeddings once with a batch, with backoff on 429/5xx.

    Returns embeddings in the same order as `inputs` (we sort by
    response.data[].index to be defensive against any future reordering).
    """
    delay = 1.0
    for attempt in range(max_retries):
        resp = _HTTP.post(
            _MISTRAL_EMBED_API_URL,
            headers={
                "Authorization": f"Bearer {_MISTRAL_API_KEY}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={"model": _MISTRAL_EMBED_MODEL, "input": inputs},
            timeout=120,
        )
        if resp.status_code < 500 and resp.status_code != 429:
            resp.raise_for_status()
            data = resp.json()["data"]
            return [d["embedding"] for d in sorted(data, key=lambda x: x.get("index", 0))]
        if attempt < max_retries - 1:
            logger.warning(
                "Mistral embed %d — retrying in %.1fs (attempt %d/%d)",
                resp.status_code, delay, attempt + 1, max_retries,
            )
            time.sleep(delay)
            delay *= 2
    raise _MistralRateLimitError(
        f"Mistral embed failed after {max_retries} retries (last status={resp.status_code})"
    )


# ── BYOK org embedding HTTP backends ──────────────────────────────────────────
# Each helper takes the API key + model + batch explicitly so the org's own
# credentials drive the call, not the platform's. Retry policy mirrors the
# Mistral path: bail on 4xx (except 429), exponential backoff on 429 / 5xx.

# Audit round 4 — A3-H4. Without a global ceiling on the retry harness,
# a saturated provider (cascading 5xx / 429) could keep a worker
# sleeping for 1s + 2s + 4s + ... per batch — and under bulk indexing
# this multiplies across N parallel batches. _MAX_BACKOFF_WALL_SECONDS
# caps the cumulative sleep so we fail fast rather than queue up
# requests behind a stuck worker. The HTTP timeout (120s) is unchanged.
_MAX_BACKOFF_WALL_SECONDS = 30.0


def _backoff_embed_post(
    url: str,
    headers: dict,
    payload: dict,
    parse: "callable",
    provider_label: str,
    max_retries: int = 3,
    max_total_seconds: float = _MAX_BACKOFF_WALL_SECONDS,
) -> list[list[float]]:
    """Shared retry harness for BYOK embed providers.

    ``parse(resp_json) -> list[list[float]]`` extracts embeddings in input order.
    Aborts when the cumulative sleep would exceed ``max_total_seconds`` —
    the worker is freed for other requests rather than waiting for a
    rate-limited provider to recover.
    """
    delay = 1.0
    last_status = None
    deadline = time.monotonic() + max_total_seconds
    for attempt in range(max_retries):
        resp = _HTTP.post(url, headers=headers, json=payload, timeout=120)
        last_status = resp.status_code
        if resp.status_code < 500 and resp.status_code != 429:
            resp.raise_for_status()
            return parse(resp.json())
        if attempt < max_retries - 1:
            now = time.monotonic()
            if now + delay > deadline:
                logger.warning(
                    "%s embed %d — backoff budget exhausted (%.1fs cap) on attempt %d/%d, aborting",
                    provider_label, resp.status_code, max_total_seconds,
                    attempt + 1, max_retries,
                )
                break
            logger.warning(
                "%s embed %d — retrying in %.1fs (attempt %d/%d, budget %.1fs left)",
                provider_label, resp.status_code, delay, attempt + 1, max_retries,
                deadline - now,
            )
            time.sleep(delay)
            delay *= 2
    raise _MistralRateLimitError(
        f"{provider_label} embed failed after {max_retries} retries (last status={last_status})"
    )


def _embed_mistral_batch_byok(
    inputs: list[str], api_key: str, model: str,
) -> list[list[float]]:
    """Mistral embedding with caller-supplied API key + model (BYOK path)."""
    return _backoff_embed_post(
        url=_MISTRAL_EMBED_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        payload={"model": model, "input": inputs},
        parse=lambda j: [
            d["embedding"]
            for d in sorted(j["data"], key=lambda x: x.get("index", 0))
        ],
        provider_label="Mistral (BYOK)",
    )


def _embed_openai_batch(
    inputs: list[str], api_key: str, model: str,
) -> list[list[float]]:
    """OpenAI /v1/embeddings — text-embedding-3-small / -3-large."""
    return _backoff_embed_post(
        url="https://api.openai.com/v1/embeddings",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        payload={"model": model, "input": inputs},
        parse=lambda j: [
            d["embedding"]
            for d in sorted(j["data"], key=lambda x: x.get("index", 0))
        ],
        provider_label="OpenAI",
    )


def _embed_voyage_batch(
    inputs: list[str], api_key: str, model: str,
) -> list[list[float]]:
    """Voyage AI /v1/embeddings — voyage-3 family.

    Voyage requires an ``input_type`` field; we pass "document" since these
    backends are used both for indexing and for query embedding. The
    asymmetric "query" variant would marginally improve query-side recall
    but adds a branch on every call — the gain doesn't justify the
    complexity at this scope.
    """
    return _backoff_embed_post(
        url="https://api.voyageai.com/v1/embeddings",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        payload={"model": model, "input": inputs, "input_type": "document"},
        parse=lambda j: [
            d["embedding"]
            for d in sorted(j["data"], key=lambda x: x.get("index", 0))
        ],
        provider_label="Voyage",
    )


def _embed_cohere_batch(
    inputs: list[str], api_key: str, model: str,
) -> list[list[float]]:
    """Cohere /v1/embed — embed-multilingual-v3.0 family.

    Cohere returns embeddings as ``embeddings.float`` (or just ``embeddings``
    on older response shapes) and requires an ``input_type``. The order of
    embeddings matches the order of inputs.
    """
    def _parse_cohere(j: dict) -> list[list[float]]:
        emb = j.get("embeddings")
        if isinstance(emb, dict):
            # New shape: {"float": [[...], ...], "int8": ...}
            return emb.get("float") or next(iter(emb.values()))
        return emb  # legacy flat list shape

    return _backoff_embed_post(
        url="https://api.cohere.com/v1/embed",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        payload={
            "model": model,
            "texts": inputs,
            "input_type": "search_document",
            "embedding_types": ["float"],
        },
        parse=_parse_cohere,
        provider_label="Cohere",
    )


def _embed_gemini_batch(
    inputs: list[str], api_key: str, model: str,
) -> list[list[float]]:
    """Google Gemini embedding — text-embedding-004 (768-dim).

    Uses the ``batchEmbedContents`` endpoint. The model name is prefixed with
    ``models/`` both in the URL and in every request entry. Auth via the
    ``x-goog-api-key`` header so the key never lands in the URL / logs. The
    returned embeddings keep the request order.
    """
    model_path = model if model.startswith("models/") else f"models/{model}"
    return _backoff_embed_post(
        url=f"https://generativelanguage.googleapis.com/v1beta/{model_path}:batchEmbedContents",
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        payload={
            "requests": [
                {"model": model_path, "content": {"parts": [{"text": t}]}}
                for t in inputs
            ]
        },
        parse=lambda j: [e["values"] for e in j["embeddings"]],
        provider_label="Gemini",
    )


_BYOK_EMBED_BACKENDS = {
    "mistral": _embed_mistral_batch_byok,
    "openai": _embed_openai_batch,
    "voyage": _embed_voyage_batch,
    "cohere": _embed_cohere_batch,
    "gemini": _embed_gemini_batch,
}


class BYOKDecryptError(Exception):
    """Raised when a user's embed_api_key_enc cannot be decrypted.

    Audit round 4 — A3-H1. The previous behaviour silently fell back to
    the deployment-wide default provider, which routed the user's
    documents through Mistral / Ollama even though the user expected their
    own BYOK provider. That meant data exposure (queries sent to the
    "wrong" provider's API) AND vector-space corruption (the personal
    collection ends up with mixed embeddings). We now propagate the
    failure so callers can refuse the operation instead.
    """

    def __init__(self, user_id: int, provider: str):
        self.user_id = user_id
        self.provider = provider
        super().__init__(
            f"User {user_id}: decryption of embed_api_key_enc failed for "
            f"provider={provider!r} — refusing to fall back to the default "
            f"provider. Re-enter the key from your settings."
        )


# Sentinel returned when a user has a BYOK provider configured but the Fernet
# decryption blew up. ``_resolve_embed_for_user`` lifts this back into a
# ``BYOKDecryptError`` so the silent fallback never happens.
_DECRYPT_FAILED = "DECRYPT_FAILED"


@functools.lru_cache(maxsize=512)
def _resolve_embed_for_user_cached(user_id: int) -> Optional[tuple]:
    """Internal LRU-cached per-user embed resolver — returns a hashable tuple.

    Reads User.embed_* columns. Personal documents live in their own
    ``user_{id}_personal`` collection, so
    a per-user embed provider/model is naturally isolated (no cross-user vector
    mixing). Returns the same 5-tuple shape, or ``None`` when the user has no
    BYOK embed key configured.
    """
    try:
        from app.ext import db as _db
        from app.models.user import User
        from app.utils.crypto import decrypt_token
    except Exception:
        return None
    u = _db.session.get(User, user_id)
    if not u:
        return None
    # Prefer an explicit embedding key. Otherwise reuse the user's SYNTHESIS key
    # when that provider also offers an embedding API (Mistral, OpenAI) — so a
    # Free user who configures a single Mistral/OpenAI key gets both answers AND
    # indexing with no extra setup. Claude/Gemini keys can't embed → no fallback.
    provider = (u.embed_provider or "").lower()
    api_key_enc = u.embed_api_key_enc
    model_override = u.embed_model
    if not provider or not api_key_enc:
        syn = (u.ai_provider or "").lower()
        if syn in _BYOK_EMBED_PROVIDERS and u.ai_api_key_enc:
            provider, api_key_enc, model_override = syn, u.ai_api_key_enc, None
        else:
            return None
    if provider not in _BYOK_EMBED_PROVIDERS:
        logger.warning(
            "User %s has unknown embed_provider=%r — falling back to default",
            user_id, provider,
        )
        return None
    try:
        api_key = decrypt_token(api_key_enc)
    except Exception as exc:
        logger.error(
            "byok_embed_decrypt_failed user=%s provider=%s: %s — refusing silent fallback",
            user_id, provider, exc,
        )
        return (provider, _DECRYPT_FAILED, None, None, None)
    model = model_override or _BYOK_EMBED_DEFAULT_MODEL[provider]
    batch_size = _BYOK_EMBED_BATCH_SIZE[provider]
    dim = _BYOK_EMBED_DIM.get((provider, model))
    return (provider, model, api_key, batch_size, dim)


def _resolve_embed_for_user(user_id: Optional[int]) -> Optional[dict]:
    """Return the BYOK embed config for a user, or None when none is set.

    Raises ``BYOKDecryptError`` when a key is configured but can't be
    decrypted, so we never silently route a user's documents through the
    wrong provider.
    """
    if user_id is None:
        return None
    cached = _resolve_embed_for_user_cached(user_id)
    if cached is None:
        return None
    provider, model, api_key, batch_size, dim = cached
    if model is _DECRYPT_FAILED:
        raise BYOKDecryptError(user_id, provider)
    return {
        "provider": provider,
        "model": model,
        "api_key": api_key,
        "batch_size": batch_size,
        "dim": dim,
    }


def invalidate_user_embed_resolution(user_id: int) -> None:
    """Drop the cached BYOK embed resolution for a user — call after writing."""
    _resolve_embed_for_user_cached.cache_clear()


def _resolve_embed_byok(user_id: Optional[int]) -> Optional[dict]:
    """Per-user BYOK embed resolution: the user's own key, or None.

    ``None`` means "no BYOK configured" — the caller then decides whether the
    deployment-wide platform embedding (Mistral/Ollama) applies, which is gated
    on the tier by ``_platform_embed_allowed``.
    """
    return _resolve_embed_for_user(user_id)


def _platform_embed_allowed(user_id: Optional[int]) -> bool:
    """May this call use the platform (Mistral/Ollama) embedding?

    The €0-on-Free rule: an individual Free user (``user_id`` set, not a
    Pro/Enterprise/admin) gets NO platform embedding — they must BYOK or not
    index. System/global indexing (newsletters, admin staging: ``user_id``
    is None) and Pro/Enterprise/admin users keep the platform default.
    Sovereign mode is local Ollama only and always allowed (no external cost).
    """
    from app.utils.deployment_mode import is_sovereign
    if is_sovereign():
        return True
    if user_id is None:
        return True
    try:
        from app.ext import db as _db
        from app.models.user import User
        u = _db.session.get(User, user_id)
        return bool(u and u.is_pro_subscriber)
    except Exception:
        return False


class EmbedKeyRequiredError(RuntimeError):
    """Raised when a Free user with no BYOK embed key tries to index/query.

    Free indexing must cost the platform €0, so there is no platform-embedding
    fallback for them — they configure their own embedding key first. Callers
    surface this as a clear 'configure your embedding key' message.
    """

    def __init__(self, user_id: Optional[int]):
        self.user_id = user_id
        super().__init__(
            f"User {user_id}: no embedding key configured and no platform "
            f"fallback on the Free tier — configure a BYOK embedding key."
        )


def _collection_first_vector_dim(col) -> Optional[int]:
    """Return the dim of the first stored vector in ``col``, or None.

    Returns None when the collection is empty or when Chroma's peek API
    behaves unexpectedly. Used by the reindex pre-flight to refuse running
    against a collection whose vectors already live in a different space
    than the active embed model would produce.
    """
    try:
        if col.count() == 0:
            return None
        probe = col.peek(limit=1)
        embs = probe.get("embeddings") if probe else None
        if embs is None:
            return None
        if len(embs) == 0 or embs[0] is None:
            return None
        return len(embs[0])
    except Exception as exc:
        logger.debug(
            "Could not probe dim on collection %s: %s",
            getattr(col, "name", "?"), exc,
        )
        return None


# Cached probe: how many floats does the current active embed model emit?
# Computed lazily on first call so a test that monkey-patches _embed never
# hits the network. Reset by _reset_active_embed_dim_cache() (tests).
_ACTIVE_EMBED_DIM_CACHE: dict = {}


def _active_embed_dim(user_id: Optional[int] = None) -> Optional[int]:
    """Return the embedding dim the currently active model emits.

    Sends a single one-character probe through ``_embed_query`` to learn
    the dim, then caches the result per (provider, model) signature.
    Used as the source of truth by reindex pre-flight checks.
    Falls back to ``_BYOK_EMBED_DIM`` when the network probe fails — the
    static table is enough for the well-known BYOK models.
    """
    key = (_active_embed_provider(user_id), _active_embed_model(user_id))
    if key in _ACTIVE_EMBED_DIM_CACHE:
        return _ACTIVE_EMBED_DIM_CACHE[key]
    static = _BYOK_EMBED_DIM.get(key)
    if static is not None:
        _ACTIVE_EMBED_DIM_CACHE[key] = static
        return static
    try:
        vec = _embed_query("d", user_id=user_id)
        dim = len(vec) if vec else None
    except Exception as exc:
        logger.warning(
            "Could not probe active embed dim for %s: %s", key, exc,
        )
        dim = None
    _ACTIVE_EMBED_DIM_CACHE[key] = dim
    return dim


def _reset_active_embed_dim_cache() -> None:
    """Clear the active-embed-dim probe cache. Mainly for tests."""
    _ACTIVE_EMBED_DIM_CACHE.clear()


def assert_collection_compat_for_reindex(col) -> None:
    """Refuse to start a reindex against an incompatible collection.

    Raises ``EmbedDimensionMismatch`` when the collection contains vectors
    whose dim differs from what the active embed model would produce —
    that's the canonical "crash mid-reindex" footprint (some chunks under
    the old space, some under the new) and updating it in place would
    create a third, mixed state. The operator must wipe the collection
    and rerun the reindex from a clean baseline.
    """
    if col is None:
        return
    existing_dim = _collection_first_vector_dim(col)
    if existing_dim is None:
        return  # empty collection — nothing to clash with
    target_dim = _active_embed_dim()
    if target_dim is None:
        return  # could not probe the active model; skip the check (best effort)
    if existing_dim != target_dim:
        raise EmbedDimensionMismatch(
            -1,
            existing={"dim": existing_dim, "model": (col.metadata or {}).get("embed_model")},
            requested={"dim": target_dim, "model": _active_embed_model()},
        )


def _embed(texts: list[str], doc_title: str = "",
           user_id: Optional[int] = None) -> list[list[float]]:
    """Embed document chunks for indexing — dispatches between Ollama / Mistral / BYOK.

    Batching avoids hitting provider request-body limits. For Mistral, also
    spaces out calls to stay below the API rate limit on bulk indexing jobs.

    For nomic-embed-text (Ollama only): applies the 'search_document:' task
    prefix (asymmetric training). If doc_title is provided it is prepended so
    the embedding reflects both document context and chunk content.
    For bge-m3, Mistral embed and other models: texts are passed as-is with
    doc_title prefix only (no task prefix needed).
    The stored chunk text remains unmodified (prefixes are only for embedding).

    When ``user_id`` resolves to a BYOK config, the user's chosen provider + key
    + model drives the call instead of the deployment-wide default.

    Audit round 4 — A3-M2. Filter out blank inputs upstream: every provider
    rejects an empty string with HTTP 400 (Mistral, OpenAI, Voyage, Cohere),
    and a wholly empty ``texts`` list returns ``[]`` so the caller doesn't
    issue a no-op request that would surface as a 400 in monitoring.
    """
    if not texts:
        return []
    # Drop blank/whitespace-only entries while remembering their positions
    # so we can re-emit a zero-vector placeholder of the right dim. (Indexing
    # callers slice by len(chunks) so the output list length must match.)
    sanitized = [t if (t is not None and t.strip()) else None for t in texts]
    valid_idx = [i for i, t in enumerate(sanitized) if t is not None]
    if not valid_idx:
        # All inputs were blank — return zero vectors with a best-effort dim.
        dim = _active_embed_dim(user_id) or 1024
        return [[0.0] * dim for _ in texts]
    filtered_texts = [sanitized[i] for i in valid_idx]

    byok = _resolve_embed_byok(user_id)
    if byok is not None:
        # BYOK path: no nomic prefix (none of the BYOK providers are nomic).
        inputs = [f"{doc_title}\n{t}".strip() if doc_title else t for t in filtered_texts]
        backend = _BYOK_EMBED_BACKENDS[byok["provider"]]
        batch_size = byok["batch_size"]
        embeddings: list[list[float]] = []
        for i in range(0, len(inputs), batch_size):
            batch = inputs[i:i + batch_size]
            embeddings.extend(backend(batch, byok["api_key"], byok["model"]))
        return _reinsert_zero_vectors(embeddings, valid_idx, len(texts), user_id)

    # No BYOK key → platform embedding (Mistral/Ollama). Gated for the €0-Free
    # rule: a Free individual user with no embed key cannot index here.
    if not _platform_embed_allowed(user_id):
        raise EmbedKeyRequiredError(user_id)

    if "nomic" in _EMBED_MODEL.lower() and not _use_mistral_embed():
        if doc_title:
            inputs = [f"search_document: {doc_title}\n{t}" for t in filtered_texts]
        else:
            inputs = [f"search_document: {t}" for t in filtered_texts]
    else:
        inputs = [f"{doc_title}\n{t}".strip() if doc_title else t for t in filtered_texts]

    use_mistral = _use_mistral_embed()
    batch_size = _MISTRAL_EMBED_BATCH_SIZE if use_mistral else _EMBED_BATCH_SIZE
    embeddings = []
    for i in range(0, len(inputs), batch_size):
        batch = inputs[i:i + batch_size]
        if use_mistral:
            embeddings.extend(_mistral_embed_post(batch))
        else:
            embeddings.extend(_embed_ollama_batch(batch))
    return _reinsert_zero_vectors(embeddings, valid_idx, len(texts), user_id)


def _reinsert_zero_vectors(
    embeddings: list[list[float]],
    valid_idx: list[int],
    total_len: int,
    user_id: Optional[int] = None,
) -> list[list[float]]:
    """Restore the original list shape after blank-input filtering.

    If ``valid_idx == list(range(total_len))`` (no blanks were dropped),
    this is a fast pass-through. Otherwise zero vectors of the appropriate
    dim are inserted so the caller's ``ids[:chunk_count]`` slicing keeps
    aligning with the original chunks.
    """
    if len(valid_idx) == total_len:
        return embeddings
    if not embeddings:
        dim = _active_embed_dim(user_id) or 1024
    else:
        dim = len(embeddings[0])
    out: list[list[float]] = [[0.0] * dim for _ in range(total_len)]
    for vec, pos in zip(embeddings, valid_idx):
        out[pos] = vec
    return out


# Embedding cache TTL — query text rarely changes meaning, but we cap at
# 1 h so a tweaked embedding model upgrade rolls through naturally
# without manual flush. Each entry is a JSON-encoded list[float] of size
# ~1024 (bge-m3), so a few KB per cached query.
_EMBED_CACHE_TTL = int(os.getenv("EMBED_CACHE_TTL", "3600"))


def _embed_cache_key(text: str, user_id: Optional[int] = None) -> str:
    """Cache key for an embed call, namespaced by provider + model.

    Including the provider in the key prevents cross-pollination when
    ``EMBED_PROVIDER`` flips at runtime or when a user BYOK kicks in — the
    same query text under bge-m3, mistral-embed and voyage-3 all collide on
    the SHA1 hash but live under distinct namespaces.

    Audit round 4 — A3-L1. NFC-normalise the text before hashing so two
    visually identical queries with different Unicode code-points hit the
    same cache entry (e.g. precomposed "é" U+00E9 vs "e" U+0065 + combining
    acute U+0301). Cheap and harmless on ASCII input.
    """
    import hashlib as _h
    import unicodedata as _ud
    provider = _active_embed_provider(user_id)
    model = _active_embed_model(user_id)
    canonical = _ud.normalize("NFC", text or "")
    return (
        f"embed:{provider}:{model}:"
        f"{_h.sha1(canonical.encode('utf-8'), usedforsecurity=False).hexdigest()}"
    )


def _embed_query(query: str, user_id: Optional[int] = None) -> list[float]:
    """Embed a search query. Applies 'search_query:' prefix for nomic-embed-text only.

    Cache layer (Redis, 1 h TTL): a typical RAG session re-uses the same
    query embedding multiple times — HyDE compares against the bare query,
    multi-turn follow-ups echo prior turns, and variants overlap with the
    original. Caching saves a 30-700 ms round-trip on every cache hit.
    The cache key includes the active embed model name so switching from
    Ollama to Mistral (or vice-versa) automatically invalidates stale
    entries. Cache failures are swallowed silently so we always fall back
    to a real embed call.

    When ``user_id`` resolves to a BYOK config, the user's provider + key
    drive the call (and the cache namespace).
    """
    byok = _resolve_embed_byok(user_id)
    # €0-Free: a Free individual user with no embed key cannot embed a query
    # against the platform either — they must BYOK first.
    if byok is None and not _platform_embed_allowed(user_id):
        raise EmbedKeyRequiredError(user_id)

    if byok is not None:
        input_text = query  # no nomic prefix on the BYOK path
    else:
        use_mistral = _use_mistral_embed()
        input_text = (
            f"search_query: {query}"
            if (not use_mistral and "nomic" in _EMBED_MODEL.lower())
            else query
        )

    cache = _get_cache() if _EMBED_CACHE_TTL > 0 else None
    cache_key = _embed_cache_key(input_text, user_id=user_id) if cache else None
    if cache and cache_key:
        try:
            hit = cache.get(cache_key)
            if hit:
                import json as _json
                return _json.loads(hit)
        except Exception as exc:
            logger.debug("embed cache read miss: %s", exc)

    if byok is not None:
        backend = _BYOK_EMBED_BACKENDS[byok["provider"]]
        embedding = backend([input_text], byok["api_key"], byok["model"])[0]
    elif _use_mistral_embed():
        embedding = _mistral_embed_post([input_text])[0]
    else:
        embedding = _embed_ollama_batch([input_text])[0]

    if cache and cache_key:
        try:
            import json as _json
            cache.set(cache_key, _json.dumps(embedding), ex=_EMBED_CACHE_TTL)
        except Exception as exc:
            logger.debug("embed cache write failed: %s", exc)

    return embedding


_CONTEXT_BATCH_SIZE = 10  # chunks per Mistral call for contextual retrieval


def _generate_chunk_contexts(doc_intro: str, chunks: list[str]) -> list[str]:
    """Generate a 1-sentence context for each chunk using a single LLM call per batch.

    Implements the Anthropic contextual retrieval technique: each chunk embedding is
    enriched with a short description of the chunk's role in the broader document.
    This reduces retrieval failure rate by ~49 % on structured documents.

    Returns a list of context strings (same length as chunks). Empty string means no
    context was generated for that chunk (used as-is for embedding).
    """
    if not _MISTRAL_API_KEY or not chunks or not doc_intro.strip():
        return [""] * len(chunks)

    import re as _re
    intro = doc_intro[:1200]

    # Build the batches up front so we can fan them out to Mistral in parallel.
    # 10 batches × 1-2s serial = 10-20s blocking call during indexing → reduce
    # to 2-3s with a small pool. Bounded at 4 to respect Mistral rate limits.
    batches = [
        chunks[i:i + _CONTEXT_BATCH_SIZE]
        for i in range(0, len(chunks), _CONTEXT_BATCH_SIZE)
    ]

    def _process_batch(batch: list) -> list:
        numbered = "\n\n".join(
            f"[{i + 1}] {chunk[:500]}"
            for i, chunk in enumerate(batch)
        )
        prompt = (
            f"Contexte du document :\n{intro}\n\n"
            f"Pour chacun des {len(batch)} extraits numérotés ci-dessous, "
            "rédige UNE phrase concise (15-25 mots) qui situe l'extrait dans le contexte "
            "du document. Format attendu : [N] ta phrase\n\n"
            f"{numbered}"
        )
        batch_contexts = [""] * len(batch)
        try:
            raw = _llm_generate(prompt, max_tokens=len(batch) * 35, temperature=0.0, timeout=30)
            for line in raw.splitlines():
                m = _re.match(r'\[(\d+)\]\s*(.*)', line.strip())
                if m:
                    idx = int(m.group(1)) - 1
                    ctx = m.group(2).strip()
                    if 0 <= idx < len(batch) and ctx:
                        batch_contexts[idx] = ctx
        except Exception as exc:
            logger.debug("Contextual retrieval batch failed: %s", exc)
        return batch_contexts

    if len(batches) <= 1:
        return _process_batch(batches[0]) if batches else []

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(len(batches), 4)) as pool:
        per_batch_contexts = list(pool.map(_process_batch, batches))

    contexts: list[str] = []
    for batch_contexts in per_batch_contexts:
        contexts.extend(batch_contexts)
    return contexts


def _contextual_embed(
    chunks: list[str],
    doc_title: str = "",
    doc_intro: str = "",
    user_id: Optional[int] = None,
) -> list[list[float]]:
    """Embed chunks enriched with per-chunk LLM context when contextual retrieval is on.

    The context sentence (what this chunk covers in the document) is prepended to each
    chunk for embedding only — stored chunk text stays unmodified so keyword search
    and display are unaffected.

    ``user_id`` propagates to ``_embed`` so BYOK indexing uses the user's
    provider + key. Contextual retrieval itself calls the platform LLM
    (Mistral), so it is SKIPPED for Free users (no platform-paid aux) — their
    chunks are embedded raw with their own embedding key.
    """
    if not _CONTEXTUAL_EMBED_ENABLED or not doc_intro or not _platform_embed_allowed(user_id):
        return _embed(chunks, doc_title=doc_title, user_id=user_id)

    contexts = _generate_chunk_contexts(doc_intro, chunks)
    enriched = [
        f"{ctx}\n\n{chunk}".strip() if ctx else chunk
        for ctx, chunk in zip(contexts, chunks)
    ]
    logger.debug(
        "Contextual retrieval: %d/%d chunks got context prefix",
        sum(1 for c in contexts if c), len(chunks),
    )
    return _embed(enriched, doc_title=doc_title, user_id=user_id)


def _parse_gemini_response(data: dict) -> str:
    """Extract text from a Gemini generateContent response, raising on blocked output.

    Handles finishReason values: STOP (normal), MAX_TOKENS (truncated but usable),
    SAFETY (content blocked), RECITATION (source grounding blocked).
    """
    candidates = data.get("candidates", [])
    if not candidates:
        # promptFeedback block (no candidates returned at all)
        block = data.get("promptFeedback", {}).get("blockReason", "UNKNOWN")
        raise ValueError(f"Gemini blocked the prompt: {block}")

    candidate = candidates[0]
    finish = candidate.get("finishReason", "STOP")

    if finish == "SAFETY":
        raise ValueError("Gemini safety filter blocked this response")
    if finish == "RECITATION":
        raise ValueError("Gemini recitation filter blocked this response")

    parts = candidate.get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()

    if finish == "MAX_TOKENS" and text:
        logger.warning("Gemini response was truncated (MAX_TOKENS) — consider raising maxOutputTokens")

    if not text:
        raise ValueError(f"Gemini returned an empty response (finishReason={finish})")

    return text


def _llm_generate(prompt: str, max_tokens: int = 120, temperature: float = 0.1, timeout: int = 30) -> str:
    """Unified LLM text generation for auxiliary tasks (HyDE, reranking, query variants).

    Routes to Mistral (small model — cheap) if configured, falls back to Ollama. The
    synthesis path uses _chat() which adds the RAG system prompt and per-user
    provider routing (and uses the medium model).
    """
    if _use_mistral_api():
        resp = _mistral_post(
            payload={
                "model": _MISTRAL_AUX_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            api_key=_MISTRAL_API_KEY,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    resp = _HTTP.post(
        f"{_OLLAMA_URL}/api/generate",
        json={
            "model": _resolved_local_model(),
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_predict": max_tokens,
                "temperature": temperature,
                "num_ctx": 4096,
            },
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


def _get_user_collection(user_id: int):
    col = _chroma_client().get_or_create_collection(
        name=f"user_{user_id}_personal",
        metadata=_hnsw_meta(),
    )
    _ensure_search_ef(col)
    return col


def index_user_document(doc_id: int, user_id: int, file_path, display_title: str) -> int:
    """Chunk, embed, upsert a personal document into the user's private collection."""
    from pathlib import Path as _Path

    from app.utils.text_extraction import chunk_text, extract_text

    text = extract_text(_Path(file_path))
    if not text.strip():
        logger.warning(f"No text extracted from user doc {doc_id}")
        return 0

    # Per-tier token budget — reject huge docs before burning embed cost.
    try:
        from app.ext import db as _db
        from app.models.user import User
        _u = _db.session.get(User, user_id)
        _budget_err = check_doc_token_budget(text, _u.abonnement_type if _u else None)
        if _budget_err:
            logger.warning("User doc %s rejected for user %s: %s", doc_id, user_id, _budget_err)
            return 0
    except Exception as _e:
        logger.debug("Budget check skipped for user_doc %s: %s", doc_id, _e)

    chunks = chunk_text(text)
    if not chunks:
        return 0

    # Document date — used by the UI freshness badge and by the temporal
    # filter ($gte/$lte on doc_ts). Falls back gracefully when missing.
    doc_date = {}
    try:
        from app.ext import db as _db
        from app.models.user_document import UserDocument
        ud = _db.session.get(UserDocument, doc_id)
        if ud:
            doc_date = _doc_date_meta(ud.uploaded_at)
    except Exception:
        pass

    ids = [f"userdoc_{doc_id}_chunk_{i}" for i in range(len(chunks))]
    metadatas = [
        {
            "source_type": "user_document",
            "user_doc_id": doc_id,
            "user_id": user_id,
            "display_title": display_title,
            "tag": "",
            "indexed_at": datetime.now(tz=timezone.utc).isoformat(),
            **doc_date,
        }
        for _ in chunks
    ]

    try:
        doc_intro = "\n\n".join(chunks[:3])
        embeddings = _contextual_embed(
            chunks, doc_title=display_title, doc_intro=doc_intro, user_id=user_id,
        )
        collection = _get_user_collection(user_id)
        collection.upsert(ids=ids, documents=chunks, embeddings=embeddings, metadatas=metadatas)
        logger.info(f"Indexed user doc {doc_id} for user {user_id}: {len(chunks)} chunks")
        return len(chunks)
    except EmbedKeyRequiredError:
        # Free tier with no embedding key — no platform fallback (€0 rule).
        logger.warning(
            "User doc %s not indexed: user %s has no embedding key (Free, no platform fallback)",
            doc_id, user_id,
        )
        return 0
    except Exception as e:
        logger.error(f"ChromaDB upsert failed for user doc {doc_id}: {e}")
        return 0


def remove_user_document_from_index(user_id: int, doc_id: int, chunk_count: int) -> None:
    if chunk_count <= 0:
        return
    ids = [f"userdoc_{doc_id}_chunk_{i}" for i in range(chunk_count)]
    try:
        collection = _get_user_collection(user_id)
        collection.delete(ids=ids)
        logger.info(f"Removed user doc {doc_id} ({chunk_count} chunks) from user {user_id} index")
    except Exception as e:
        logger.error(f"Failed to remove user doc {doc_id} from index: {e}")


def index_cloud_document(cloud_doc_id: int, user_id: int, file_path, display_title: str) -> int:
    """Chunk, embed, upsert a cloud-synced document into the user's private collection."""
    from pathlib import Path as _Path

    from app.utils.text_extraction import chunk_text, extract_text

    text = extract_text(_Path(file_path))
    if not text.strip():
        logger.warning(f"No text extracted from cloud doc {cloud_doc_id}")
        return 0

    # Per-tier token budget — reject huge docs before burning embed cost.
    try:
        from app.ext import db as _db
        from app.models.user import User
        _u = _db.session.get(User, user_id)
        _budget_err = check_doc_token_budget(text, _u.abonnement_type if _u else None)
        if _budget_err:
            logger.warning("Cloud doc %s rejected for user %s: %s", cloud_doc_id, user_id, _budget_err)
            return 0
    except Exception as _e:
        logger.debug("Budget check skipped for cloud_doc %s: %s", cloud_doc_id, _e)

    chunks = chunk_text(text)
    if not chunks:
        return 0

    # Look up the cloud doc to capture its provider web_url (Drive
    # webViewLink, OneDrive webUrl, etc.) into the chunk metadata so the
    # RAG source list can render a clickable link back to the original,
    # and its remote_modified_at for the freshness badge / date filter.
    web_url = ""
    doc_date = {}
    try:
        from app.ext import db as _db
        from app.models.cloud_connection import CloudDocument
        cd = _db.session.get(CloudDocument, cloud_doc_id)
        if cd:
            if cd.web_url:
                web_url = cd.web_url
            doc_date = _doc_date_meta(cd.remote_modified_at)
    except Exception:
        pass

    ids = [f"clouddoc_{cloud_doc_id}_chunk_{i}" for i in range(len(chunks))]
    metadatas = [
        {
            "source_type": "cloud_document",
            "cloud_doc_id": cloud_doc_id,
            "user_id": user_id,
            "display_title": display_title,
            "tag": "",
            "web_url": web_url,
            "indexed_at": datetime.now(tz=timezone.utc).isoformat(),
            **doc_date,
        }
        for _ in chunks
    ]

    try:
        doc_intro = "\n\n".join(chunks[:3])
        embeddings = _contextual_embed(
            chunks, doc_title=display_title, doc_intro=doc_intro, user_id=user_id,
        )
        collection = _get_user_collection(user_id)
        collection.upsert(ids=ids, documents=chunks, embeddings=embeddings, metadatas=metadatas)
        logger.info(f"Indexed cloud doc {cloud_doc_id} for user {user_id}: {len(chunks)} chunks")
        return len(chunks)
    except EmbedKeyRequiredError:
        logger.warning(
            "Cloud doc %s not indexed: user %s has no embedding key (Free, no platform fallback)",
            cloud_doc_id, user_id,
        )
        return 0
    except Exception as e:
        logger.error(f"ChromaDB upsert failed for cloud doc {cloud_doc_id}: {e}")
        return 0


def remove_cloud_document_from_index(user_id: int, cloud_doc_id: int, chunk_count: int) -> None:
    if chunk_count <= 0:
        return
    ids = [f"clouddoc_{cloud_doc_id}_chunk_{i}" for i in range(chunk_count)]
    try:
        collection = _get_user_collection(user_id)
        collection.delete(ids=ids)
        logger.info(f"Removed cloud doc {cloud_doc_id} ({chunk_count} chunks) from user {user_id} index")
    except Exception as e:
        logger.error(f"Failed to remove cloud doc {cloud_doc_id} from index: {e}")


def get_user_index_summary(user_id: int) -> list[dict]:
    """Return deduplicated indexed sources from the user's personal ChromaDB collection.

    Each entry: {source_type, source_id, title, chunk_count}
    """
    try:
        collection = _get_user_collection(user_id)
        result = collection.get(include=["metadatas"])
        sources: dict[tuple, dict] = {}
        for meta in result.get("metadatas") or []:
            stype = meta.get("source_type", "unknown")
            if stype == "user_document":
                key = ("user_document", str(meta.get("user_doc_id", "")))
                if key not in sources:
                    sources[key] = {"source_type": stype, "source_id": key[1],
                                    "title": meta.get("display_title", "—"), "chunk_count": 0}
            elif stype == "cloud_document":
                key = ("cloud_document", str(meta.get("cloud_doc_id", "")))
                if key not in sources:
                    sources[key] = {"source_type": stype, "source_id": key[1],
                                    "title": meta.get("display_title", "—"), "chunk_count": 0}
            elif stype == "user_scraper_article":
                key = ("user_scraper_article", str(meta.get("scraper_article_id", "")))
                if key not in sources:
                    sources[key] = {"source_type": stype, "source_id": key[1],
                                    "title": meta.get("article_title", "—"), "chunk_count": 0}
            else:
                continue
            sources[key]["chunk_count"] += 1
        return sorted(sources.values(), key=lambda x: x["source_type"])
    except Exception as e:
        logger.error(f"get_user_index_summary({user_id}): {e}")
        return []


def delete_user_indexed_source(user_id: int, source_type: str, source_id: str) -> int:
    """Delete all ChromaDB chunks for a given source from the user's personal collection.

    Returns number of chunks deleted, or -1 on error.
    """
    _PREFIX = {
        "user_document": "userdoc",
        "cloud_document": "clouddoc",
        "user_scraper_article": "userscraper",
    }
    if source_type not in _PREFIX:
        return 0
    try:
        collection = _get_user_collection(user_id)
        result = collection.get(
            where={"source_type": source_type},
            include=["metadatas"],
        )
        field_map = {
            "user_document": "user_doc_id",
            "cloud_document": "cloud_doc_id",
            "user_scraper_article": "scraper_article_id",
        }
        field = field_map[source_type]
        ids_to_delete = [
            doc_id
            for doc_id, meta in zip(result.get("ids") or [], result.get("metadatas") or [])
            if str(meta.get(field, "")) == source_id
        ]
        if ids_to_delete:
            collection.delete(ids=ids_to_delete)
        logger.info(f"Deleted {len(ids_to_delete)} chunks ({source_type}/{source_id}) for user {user_id}")
        return len(ids_to_delete)
    except Exception as e:
        logger.error(f"delete_user_indexed_source({user_id}, {source_type}, {source_id}): {e}")
        return -1


def delete_user_collection(user_id: int) -> None:
    """Drop the personal ChromaDB collection for a user (called on account deletion)."""
    import chromadb
    try:
        client = chromadb.PersistentClient(path=str(_CHROMA_PATH))
        client.delete_collection(f"user_{user_id}_personal")
        logger.info(f"Deleted personal collection for user {user_id}")
    except Exception as e:
        logger.debug(f"delete_user_collection({user_id}): {e}")


_AI_PROVIDER_DEFAULTS = {
    "mistral": "mistral-medium-latest",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
    "claude": "claude-haiku-4-5-20251001",
}


def _chat_ollama(user_msg: str, max_tokens: int = 1024,
                 system_prompt: str = _RAG_SYSTEM_PROMPT_STRICT) -> str:
    resp = _HTTP.post(
        f"{_OLLAMA_URL}/api/chat",
        json={
            "model": _resolved_local_model(),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            "stream": False,
            "options": {
                "temperature": 0.1,
                "num_predict": max_tokens,
                # Enough context for 8 × 1500-char sources + system prompt + query.
                "num_ctx": 8192,
            },
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


def _chat_mistral(user_msg: str, api_key: str, model: str, max_tokens: int = 1024,
                  system_prompt: str = _RAG_SYSTEM_PROMPT_STRICT) -> str:
    resp = _mistral_post(
        payload={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        },
        api_key=api_key,
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _chat_openai(user_msg: str, api_key: str, model: str, max_tokens: int = 1024,
                 system_prompt: str = _RAG_SYSTEM_PROMPT_STRICT) -> str:
    """OpenAI chat completion with exponential-backoff retry on 429.

    Same retry contract as Mistral / Gemini — under transient rate-limit
    pressure the call retries (2 s, 4 s, 8 s) before giving up. On final
    failure the generic _chat router catches the Exception and falls
    back to the platform Mistral key, then Ollama.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    delay = 2.0
    max_retries = 3
    for attempt in range(max_retries):
        resp = _HTTP.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers, json=payload, timeout=60,
        )
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        if attempt < max_retries - 1:
            logger.warning("OpenAI 429 — retrying in %.1fs (attempt %d/%d)",
                           delay, attempt + 1, max_retries)
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"OpenAI rate limit after {max_retries} retries")


def _chat_gemini(user_msg: str, api_key: str, model: str, max_tokens: int = 1024,
                 system_prompt: str = _RAG_SYSTEM_PROMPT_STRICT) -> str:
    resp = _gemini_post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": api_key},
        payload={
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"parts": [{"text": user_msg}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": max_tokens},
            "safetySettings": _GEMINI_SAFETY_SETTINGS,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return _parse_gemini_response(resp.json())


def _chat_claude(user_msg: str, api_key: str, model: str, max_tokens: int = 1024,
                 system_prompt: str = _RAG_SYSTEM_PROMPT_STRICT) -> str:
    """Anthropic Claude chat with exponential-backoff retry on 429.

    Same retry contract as Mistral / Gemini / OpenAI. On final failure
    the generic _chat router falls back to the platform Mistral key,
    then Ollama.
    """
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_msg}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

    delay = 2.0
    max_retries = 3
    for attempt in range(max_retries):
        resp = _HTTP.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers, json=payload, timeout=60,
        )
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]
        if attempt < max_retries - 1:
            logger.warning("Claude 429 — retrying in %.1fs (attempt %d/%d)",
                           delay, attempt + 1, max_retries)
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"Claude rate limit after {max_retries} retries")


_BYOK_NO_KEY_ANSWER = (
    "⚠️ Aucune clé API IA n'est configurée pour votre compte. Ajoutez votre "
    "propre clé (Anthropic Claude, OpenAI, Google Gemini ou Mistral) dans "
    "Paramètres → Fournisseur IA pour générer des réponses."
)
_BYOK_NO_KEY_WARNING = (
    "Configurez votre clé API dans vos paramètres pour activer la synthèse IA."
)


def _dispatch_provider(provider, user_msg, api_key, model, max_tokens, system_prompt):
    """Call the provider-specific chat function. Raises on unknown provider."""
    if provider == "mistral":
        return _chat_mistral(user_msg, api_key, model, max_tokens, system_prompt=system_prompt)
    if provider == "openai":
        return _chat_openai(user_msg, api_key, model, max_tokens, system_prompt=system_prompt)
    if provider == "gemini":
        return _chat_gemini(user_msg, api_key, model, max_tokens, system_prompt=system_prompt)
    if provider == "claude":
        return _chat_claude(user_msg, api_key, model, max_tokens, system_prompt=system_prompt)
    raise ValueError(f"Unknown AI provider: {provider}")


def _resolve_byok(user_id: Optional[int]):
    """Resolve the BYOK provider config in strict priority order:

      1. the USER's own key (User.ai_provider + ai_api_key_enc set) → decrypt,
         model = user.ai_model or the provider default.
      2. the platform Mistral key, ONLY IF the user is Pro/Enterprise/admin
         (``is_pro_subscriber``) AND a platform key is configured. Free users
         get NO platform fallback — they must BYOK to get any synthesis.

    Returns (provider, api_key, model, source) or None when no key is
    available. There is deliberately NO Ollama fallback here — sovereign mode
    (the only Ollama path) is short-circuited by the caller before we get here.
    """
    from app.ext import db as _db
    from app.utils.crypto import decrypt_token

    if user_id is not None:
        from app.models.user import User
        u = _db.session.get(User, user_id)
        if u and u.ai_provider and u.ai_api_key_enc:
            model = u.ai_model or _AI_PROVIDER_DEFAULTS.get(u.ai_provider, "")
            return (u.ai_provider, decrypt_token(u.ai_api_key_enc), model, f"user:{user_id}")

    # Platform Mistral fallback — GATED on the Pro tier. Pro/Enterprise/admin
    # users get the included platform Mistral; Free users get nothing (None →
    # the caller tells them to configure their own key).
    if _use_mistral_api() and user_id is not None:
        from app.models.user import User
        u = _db.session.get(User, user_id)
        if u and u.is_pro_subscriber:
            return ("mistral", _MISTRAL_API_KEY, _MISTRAL_LLM_MODEL, "platform")

    return None


def _chat(
    user_msg: str,
    user_id: Optional[int] = None,
    max_tokens: int = 1024,
    strict_mode: bool = True,
) -> tuple[str, Optional[str]]:
    """Route the generation call following strict, tier-aware BYOK resolution.

    Returns (answer_text, warning_message). warning_message is set when no key
    is configured or the provider errored.

    Priority (via _resolve_byok): user key → platform Mistral (only
    for Pro/Enterprise/admin, only if a platform key is configured). There is
    NO silent Ollama fallback and NO platform fallback for Free users — if no
    key resolves the user is told to configure one, and a provider error
    surfaces as-is (pure BYOK: we never quietly answer with someone else's
    model).

    Sovereign mode is the sole exception: it runs entirely on local Ollama by
    design (air-gap), short-circuiting BYOK so no query can leave the server.

    strict_mode=True (default) uses the strict source-only prompt;
    strict_mode=False uses the relaxed prompt that allows clearly-flagged
    general-knowledge backfill.
    """
    sp = _RAG_SYSTEM_PROMPT_STRICT if strict_mode else _RAG_SYSTEM_PROMPT_RELAXED

    from app.utils.deployment_mode import is_sovereign
    if is_sovereign():
        return _chat_ollama(user_msg, max_tokens, system_prompt=sp), None

    try:
        resolved = _resolve_byok(user_id)
    except Exception as e:
        logger.error("Failed to resolve BYOK provider (user=%s): %s", user_id, e)
        resolved = None

    if resolved is None:
        return _BYOK_NO_KEY_ANSWER, _BYOK_NO_KEY_WARNING

    provider, api_key, model, source = resolved
    from app.utils.ai_providers import PROVIDER_LABELS
    provider_label = PROVIDER_LABELS.get(provider, provider.capitalize())

    try:
        return _dispatch_provider(provider, user_msg, api_key, model, max_tokens, sp), None
    except _MistralRateLimitError:
        logger.warning("Mistral rate-limited for %s — no fallback (pure BYOK)", source)
        return (
            "⚠️ Le quota de votre clé Mistral est temporairement atteint. "
            "Réessayez dans un instant.",
            "Quota Mistral atteint — aucune réponse générée.",
        )
    except Exception as e:
        logger.error("BYOK provider %s failed (%s): %s", provider, source, e)
        warning = (
            f"Le fournisseur IA ({provider_label}) a retourné une erreur "
            f"({type(e).__name__}). Vérifiez votre clé API et votre quota."
        )
        return (
            f"⚠️ Impossible de générer une réponse via {provider_label}. {warning}",
            warning,
        )


_SUMMARY_SYSTEM_PROMPT = (
    "Tu es un assistant médical pour médecins généralistes. Résume l'article "
    "fourni en 2 à 3 phrases concises, factuelles et en français, en mettant "
    "en avant l'information cliniquement pertinente. N'invente rien ; n'ajoute "
    "ni introduction ni conclusion, donne uniquement le résumé."
)


def can_summarize_for_user(user) -> bool:
    """True when a digest AI summary is producible for *user*.

    Mirrors the resolution in ``summarize_for_user`` / ``_resolve_byok``:
    either the user has their OWN BYOK key (which always takes priority), or
    they are Pro/Enterprise/admin (``is_pro_subscriber``) and the platform
    Mistral key is available. Free users with no personal key get nothing.
    Used to gate the per-user digest summary in the recap cron so Pro users
    don't need to BYOK to get summaries (the included Mistral covers them).
    """
    if getattr(user, "has_ai_key", False):
        return True
    return _use_mistral_api() and bool(getattr(user, "is_pro_subscriber", False))


def summarize_for_user(text: str, user_id: int, max_tokens: int = 220):
    """Summarise *text* for a user's digest via the BYOK resolution chain.

    Resolution (see ``_resolve_byok``): the user's OWN key first (priority),
    otherwise the platform Mistral key — but ONLY for Pro/Enterprise/admin
    (``is_pro_subscriber``). Free users with no personal key get None. Returns
    the summary string, or None when no key resolves or the provider errors
    (best-effort — the digest then falls back to the raw article excerpt).
    There is no Ollama fallback off-sovereign.
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        resolved = _resolve_byok(user_id)
    except Exception as e:
        logger.warning("Digest summary: BYOK resolve failed for user %s: %s", user_id, e)
        return None
    if resolved is None:
        return None
    provider, api_key, model, _ = resolved
    try:
        summary = _dispatch_provider(
            provider, text[:6000], api_key, model, max_tokens, _SUMMARY_SYSTEM_PROMPT,
        )
        return (summary or "").strip() or None
    except Exception as e:
        logger.warning("Digest summary failed for user %s (%s): %s", user_id, provider, e)
        return None


def index_newsletter(newsletter_id: int) -> int:
    """
    Indexe toutes les sections d'une newsletter dans ChromaDB.
    Upsert : ré-indexer une newsletter déjà indexée est sans danger.
    Retourne le nombre de sections indexées.
    """
    from app.ext import db
    from app.models import Newsletter

    newsletter = db.session.get(Newsletter, newsletter_id)
    if not newsletter or not newsletter.sections:
        logger.info(f"Newsletter {newsletter_id} : rien à indexer")
        return 0

    ids, documents, metadatas = [], [], []

    for section in newsletter.sections:
        plain = _strip_html(section.content)
        if len(plain.strip()) < 30:
            continue
        document = f"{section.title}\n\n{plain}"
        ids.append(f"section_{section.id}")
        documents.append(document)
        metadatas.append({
            "section_id": section.id,
            "newsletter_id": newsletter_id,
            "newsletter_date": str(newsletter.date),
            "newsletter_title": newsletter.title or "",
            "section_title": section.title or "",
            "tag": section.tag or "",
            "indexed_at": datetime.now(tz=timezone.utc).isoformat(),
            **_doc_date_meta(newsletter.date),
        })

    if not documents:
        logger.info(f"Newsletter {newsletter_id} : sections trop courtes, ignorées")
        return 0

    try:
        # Pass the newsletter title so each section embedding reflects the publication context,
        # improving retrieval for queries that mention the newsletter name or topic area.
        embeddings = _embed(documents, doc_title=newsletter.title or "")
        collection = _get_collection()
        collection.upsert(ids=ids, documents=documents, embeddings=embeddings, metadatas=metadatas)
        logger.info(f"Newsletter {newsletter_id} : {len(documents)} sections indexées")
        return len(documents)
    except Exception as e:
        logger.error(f"Erreur indexation newsletter {newsletter_id}: {e}")
        return 0


def clear_index() -> None:
    """Supprime et recrée la collection ChromaDB (nécessaire après changement de modèle d'embedding)."""
    import chromadb
    _CHROMA_PATH.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(_CHROMA_PATH))
    try:
        client.delete_collection(_COLLECTION_NAME)
        logger.info("Collection ChromaDB supprimée")
    except Exception as e:  # noqa: BLE001 — collection may not exist yet
        logger.debug(f"Suppression collection ignorée: {e}")
    client.get_or_create_collection(
        name=_COLLECTION_NAME,
        metadata=_hnsw_meta(),
    )
    logger.info("Collection ChromaDB recréée")


def index_all_newsletters(clear_first: bool = False) -> dict:
    """Indexation rétroactive de toutes les newsletters publiées."""
    from app.models import Newsletter

    if clear_first:
        clear_index()

    newsletters = Newsletter.query.filter_by(status="sent").all()
    total_sections = 0
    total_newsletters = 0

    for nl in newsletters:
        n = index_newsletter(nl.id)
        if n:
            total_sections += n
            total_newsletters += 1

    logger.info(
        f"Indexation complète : {total_sections} sections sur {total_newsletters} newsletters"
    )
    return {"newsletters": total_newsletters, "sections": total_sections}


def get_index_stats() -> dict:
    try:
        collection = _get_collection()
        return {
            "indexed_sections": collection.count(),
            "embed_model": _EMBED_MODEL,
            "llm_model": _LLM_MODEL,
            "status": "ok",
        }
    except Exception as e:
        return {"indexed_sections": 0, "status": f"erreur: {e}"}


def index_raw_article(article: dict) -> bool:
    """Embed a single staged article directly into ChromaDB.

    Called by the admin RAG staging UI when an article is approved.
    Returns True on success, False on failure (Ollama unavailable, etc.).
    """
    import hashlib

    titre = article.get("titre", "")
    summary = article.get("summary") or article.get("resume") or ""
    lien = article.get("lien", "")
    document = f"{titre}\n\n{summary}".strip() if summary else titre
    if not document:
        logger.warning("index_raw_article: article vide, ignoré")
        return False

    key = lien or titre
    doc_id = f"article_{hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()[:16]}"

    try:
        embeddings = _embed([document])
        collection = _get_collection()
        collection.upsert(
            ids=[doc_id],
            documents=[document],
            embeddings=embeddings,
            metadatas=[{
                "source_type": "staged_article",
                "titre": titre[:255],
                "source": article.get("source", ""),
                "lien": lien,
                "score": int(article.get("score", 0)),
                "category": article.get("category", ""),
                "tag": article.get("theme", ""),
                "indexed_at": datetime.now(tz=timezone.utc).isoformat(),
                **_doc_date_meta(
                    article.get("published_at") or article.get("date") or datetime.now(tz=timezone.utc)
                ),
            }],
        )
        logger.info(f"Article indexé dans ChromaDB : {titre[:60]}")
        return True
    except Exception as e:
        logger.error(f"Erreur indexation article '{titre[:40]}': {e}")
        return False


_STOPWORDS = frozenset({
    # ── French articles, prepositions, pronouns ───────────────────────────────
    "le", "la", "les", "l", "un", "une", "des", "du", "au", "aux",
    "de", "du", "en", "à", "par", "pour", "sur", "sous", "dans", "entre",
    "vers", "avec", "sans", "après", "avant", "depuis", "pendant", "contre",
    "selon", "parmi", "malgré", "sauf", "hors", "lors", "dès", "chez",
    # Pronouns / determiners
    "je", "tu", "il", "elle", "nous", "vous", "ils", "elles", "on",
    "me", "te", "se", "lui", "eux", "leur", "leurs",
    "mon", "ton", "son", "ma", "ta", "sa", "mes", "tes", "ses",
    "notre", "votre", "ce", "cet", "cette", "ces", "cela", "ça",
    "qui", "que", "quoi", "dont", "où", "lequel", "laquelle", "lesquels",
    "lesquelles", "duquel", "auquel",
    # Conjunctions / adverbs
    "et", "ou", "ni", "mais", "or", "donc", "car", "puis", "si", "comme",
    "quand", "lorsque", "puisque", "bien", "très", "aussi", "encore",
    "même", "ainsi", "alors", "ensuite", "enfin", "déjà", "toujours",
    "jamais", "souvent", "parfois", "peu", "plus", "moins", "trop",
    "assez", "plutôt", "surtout", "notamment", "seulement", "vraiment",
    # Common verbs (conjugated forms)
    "est", "sont", "être", "avoir", "été", "fait", "faire", "peut",
    "peuvent", "doit", "doivent", "faut", "aller", "venir", "dire",
    "voir", "savoir", "prendre", "donner", "permettre", "permettent",
    "utiliser", "utilise", "utilisent", "concerne", "concernent",
    # Quantifiers / numbers
    "tout", "tous", "toute", "toutes", "aucun", "aucune", "chaque",
    "plusieurs", "différents", "différentes", "certains", "certaines",
    "deux", "trois", "quatre", "cinq", "dix", "cent", "mille",
    # Generic nouns (too vague for keyword search)
    "cas", "fait", "fois", "type", "types", "niveau", "niveaux",
    "point", "points", "partie", "parties", "exemple", "exemples",
    "aspect", "aspects", "élément", "éléments", "ensemble", "résultat",
    "résultats", "problème", "problèmes", "question", "questions",
    "moyen", "moyens", "manière", "façon", "rapport", "rapports",
    # ── English stopwords ─────────────────────────────────────────────────────
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "into", "through", "during",
    "is", "are", "was", "were", "be", "been", "being", "have", "has",
    "had", "do", "does", "did", "will", "would", "shall", "should",
    "may", "might", "must", "can", "could", "not", "no", "nor",
    "this", "that", "these", "those", "it", "its", "their", "our",
    "your", "his", "her", "we", "they", "who", "which", "what", "how",
    "when", "where", "all", "each", "any", "some", "more", "most",
    "also", "only", "very", "just", "about", "than", "then", "there",
    "both", "such", "used", "use", "using", "other", "others",
})


def _extract_keywords(query: str) -> list[str]:
    """Extract significant unigrams and bigrams from a query for sparse retrieval.

    Unigrams: words ≥ 4 chars not in the stopword list.
    Bigrams: pairs of consecutive significant unigrams (improves phrase matching).
    """
    import re as _re
    tokens = _re.findall(r'\b[a-zA-ZÀ-ÿ]{3,}\b', query)
    unigrams = [w for w in dict.fromkeys(tokens) if w.lower() not in _STOPWORDS]

    # Bigrams from consecutive tokens (order preserved, same dedup logic)
    all_tokens = [t for t in tokens if t.lower() not in _STOPWORDS]
    bigrams = [
        f"{all_tokens[i]} {all_tokens[i + 1]}"
        for i in range(len(all_tokens) - 1)
    ]
    bigrams = list(dict.fromkeys(bigrams))

    # Unigrams first (higher recall), then bigrams (higher precision)
    return unigrams + bigrams


def _query_collection(collection, query_embedding, top_k: int, where=None):
    """Run a ChromaDB similarity query, return (docs, metas, distances) or ([], [], [])."""
    try:
        n = min(top_k, collection.count())
        if n == 0:
            return [], [], []
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=n,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        return results["documents"][0], results["metadatas"][0], results["distances"][0]
    except Exception as e:
        logger.error(f"Erreur requête ChromaDB ({collection.name}): {e}")
        return [], [], []


def _hybrid_query_collection(
    collection,
    query_embedding: list[float],
    query_text: str,
    n: int,
    where: dict = None,
) -> tuple[list, list, list]:
    """
    Reciprocal Rank Fusion of dense (embedding) + sparse (keyword) searches.

    Dense pass  : ChromaDB cosine similarity, top 3n candidates.
    Sparse pass : one ChromaDB query per keyword (where_document $contains),
                  also ranked by embedding similarity, top 2n candidates each.
    Fusion      : RRF with k=60 — documents appearing in both lists get a
                  score boost; the original semantic distance is preserved for
                  the relevance % shown in the UI.
    """
    _K_RRF = 60
    # id → [doc, meta, semantic_dist, rrf_score]
    scored: dict[str, list] = {}

    # ── Dense pass ──────────────────────────────────────────────────────────
    try:
        dense_n = min(n * 3, max(collection.count(), 1))
        r = collection.query(
            query_embeddings=[query_embedding],
            n_results=dense_n,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        for rank, (id_, doc, meta, dist) in enumerate(zip(
            r["ids"][0], r["documents"][0], r["metadatas"][0], r["distances"][0]
        )):
            scored[id_] = [doc, meta, dist, 1.0 / (_K_RRF + rank)]
    except Exception as exc:
        logger.error("Hybrid dense query failed on %s: %s", collection.name, exc)

    # ── Sparse pass (one query per keyword, fanned out in parallel) ─────────
    # ChromaDB's local SQLite-backed client is thread-safe for read queries;
    # network clients use HTTP under the hood. 8 keywords × ~150ms serial =
    # ~1.2s on the hot path; parallel = ~150-300ms.
    keywords = _extract_keywords(query_text)[:8]
    if keywords:
        from concurrent.futures import ThreadPoolExecutor

        sparse_n = min(n * 2, max(collection.count(), 1))

        def _sparse_query(word: str):
            try:
                return word, collection.query(
                    query_embeddings=[query_embedding],
                    n_results=sparse_n,
                    where=where,
                    where_document={"$contains": word},
                    include=["documents", "metadatas", "distances"],
                ), None
            except Exception as exc:
                return word, None, exc

        with ThreadPoolExecutor(max_workers=min(len(keywords), 8)) as pool:
            sparse_results = list(pool.map(_sparse_query, keywords))

        for word, r, exc in sparse_results:
            if exc is not None or r is None:
                if exc is not None:
                    # A3-L3 — the keyword itself isn't deeply private (it's
                    # a single token a sparse-index query stumbled on) but
                    # we keep it out of DEBUG logs by default for consistency.
                    logger.debug("Hybrid sparse query keyword skipped: %s", exc)
                continue
            for rank, (id_, doc, meta, dist) in enumerate(zip(
                r["ids"][0], r["documents"][0], r["metadatas"][0], r["distances"][0]
            )):
                rrf = 1.0 / (_K_RRF + rank)
                if id_ in scored:
                    scored[id_][3] += rrf          # accumulate RRF score
                else:
                    scored[id_] = [doc, meta, dist, rrf]

    if not scored:
        return [], [], [], []

    # Sort by RRF score (descending), take top-n
    ranked = sorted(scored.values(), key=lambda x: x[3], reverse=True)[:n]
    docs      = [x[0] for x in ranked]
    metas     = [x[1] for x in ranked]
    dists     = [x[2] for x in ranked]   # original semantic distance → relevance %
    rrf_scores = [x[3] for x in ranked]
    return docs, metas, dists, rrf_scores


_HYDE_QUESTION_WORDS = frozenset({
    "comment", "pourquoi", "quand", "où", "qui", "quoi", "que", "quel", "quelle", "quelles",
    "quels", "combien", "laquelle", "lequel", "lesquels", "lesquelles", "expliquer", "décrire",
    "how", "why", "when", "where", "who", "what", "which", "explain", "describe",
})


def _should_use_hyde(query: str) -> bool:
    """HyDE adds value for long, conversational queries but costs an extra LLM call.
    Skip it for short keyword lookups (< 25 chars) or when no question words are detected.
    """
    if len(query.strip()) < 20:
        return False
    words = query.lower().split()
    return any(w in _HYDE_QUESTION_WORDS for w in words)


def _trigram_set(text: str) -> set[str]:
    t = text.lower()
    return {t[i:i + 3] for i in range(len(t) - 2)}


def _text_sim(a: str, b: str) -> float:
    """Jaccard similarity on character trigrams — fast, language-agnostic."""
    ta, tb = _trigram_set(a), _trigram_set(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return inter / (len(ta) + len(tb) - inter)


def _mmr_select(
    rows: list[tuple],
    top_k: int,
    lambda_: float = 0.65,
) -> list[tuple]:
    """Maximum Marginal Relevance: balance relevance vs. diversity.

    lambda_=1 → pure relevance (same as sorted order).
    lambda_=0 → pure diversity.
    Default 0.65 keeps a strong relevance bias while removing near-duplicate chunks.
    Slightly higher than 0.6 because the larger RETRIEVAL_K pool already exposes
    more diverse candidates — we can afford to bias a bit more toward relevance.
    Uses the RRF score (index 3) as the relevance signal and character-trigram
    Jaccard as the text similarity measure.
    """
    if len(rows) <= top_k:
        return rows

    scores = [r[3] for r in rows]
    max_s = max(scores) or 1.0
    norm = [s / max_s for s in scores]

    selected: list[int] = []
    remaining = list(range(len(rows)))

    while len(selected) < top_k and remaining:
        best_idx, best_val = None, -1.0
        for idx in remaining:
            relevance = norm[idx]
            redundancy = (
                max(_text_sim(rows[idx][0], rows[j][0]) for j in selected)
                if selected else 0.0
            )
            val = lambda_ * relevance - (1 - lambda_) * redundancy
            if val > best_val:
                best_val = val
                best_idx = idx
        if best_idx is None:
            break
        selected.append(best_idx)
        remaining.remove(best_idx)

    return [rows[i] for i in selected]


def _hyde_embed(query: str) -> list[float]:
    """
    Hypothetical Document Embedding (HyDE).

    Asks the LLM to generate a short passage that would contain the answer to
    the query, then embeds that passage with the 'search_document:' task prefix.
    The final embedding is the average of this hypothetical-document embedding
    and the regular search_query: embedding, which gives the best of both:
    — the query embedding anchors the search to the right topic
    — the hypothetical embedding pulls toward the document surface form

    Performance:
    — The combined embedding is cached in Redis (1 h TTL): a cache hit
      skips the LLM call + both embed calls outright (saves 1-15 s).
    — On a cache miss, the plain query embed runs in parallel with the
      LLM hypothetical generation (the bare embed doesn't depend on the
      hypothetical text), saving the 200-700 ms it used to add serially.

    Falls back to a plain query embedding on any error. Runs on the platform
    default embedding space (the ``shared`` library), so no per-user BYOK
    plumbing is needed here.
    """
    import json as _json
    from concurrent.futures import ThreadPoolExecutor

    cache = _get_cache() if _EMBED_CACHE_TTL > 0 else None
    cache_key = (
        f"hyde:{_active_embed_provider()}:{_active_embed_model()}:"
        f"{_embed_cache_key(query).rsplit(':', 1)[1]}"
        if cache else None
    )
    if cache and cache_key:
        try:
            hit = cache.get(cache_key)
            if hit:
                return _json.loads(hit)
        except Exception as exc:
            logger.debug("hyde cache read miss: %s", exc)

    prompt = (
        "Generate a short document excerpt (2-3 sentences) that would contain "
        "the answer to the following question. The excerpt must look like an actual "
        "factual document passage — not a direct answer. "
        "Reply in the same language as the question. "
        "Output only the passage, no introduction.\n\n"
        f"Question: {query}"
    )

    # Run plain query embed and hypothetical generation in parallel —
    # neither depends on the other.
    plain_embed: Optional[list[float]] = None
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_q = pool.submit(_embed_query, query)
            # Audit round 4 — A3-L2 (debt). Trimmed from 20s → 15s so the
            # worst-case search wall-time (HyDE 15s + rerank 30s +
            # variants 10s ≈ 55s) stays under a reasonable user-facing
            # ceiling without a full deadline plumbing refactor.
            fut_hyp = pool.submit(_llm_generate, prompt,
                                  max_tokens=120, temperature=0.1, timeout=15)
            try:
                hypothetical = fut_hyp.result()
            except Exception as exc:
                logger.debug("HyDE LLM failed, using plain query embed: %s", exc)
                # Reuse the parallel-fetched plain embed instead of re-calling.
                return fut_q.result()

            plain_embed = fut_q.result()

            if hypothetical:
                emb_h = _embed([hypothetical])[0]
                combined = [(a + b) / 2.0 for a, b in zip(plain_embed, emb_h)]
                # Audit round 4 — A3-L3. Never log raw user-generated content
                # at DEBUG level (RGPD risk if DEBUG is accidentally enabled
                # in production). The length and a hash prefix are enough to
                # correlate without exposing the actual passage.
                import hashlib as _h
                logger.debug(
                    "HyDE hypothetical generated len=%d hash=%s",
                    len(hypothetical),
                    _h.sha1(hypothetical.encode("utf-8"), usedforsecurity=False).hexdigest()[:8],
                )
                if cache and cache_key:
                    try:
                        cache.set(cache_key, _json.dumps(combined), ex=_EMBED_CACHE_TTL)
                    except Exception as exc:
                        logger.debug("hyde cache write failed: %s", exc)
                return combined
    except Exception as exc:
        logger.debug("HyDE pipeline failed, using plain query embedding: %s", exc)

    # Reach this when hypothetical was empty or an outer-pool exception bubbled.
    return plain_embed if plain_embed is not None else _embed_query(query)


def _rerank(
    query: str,
    rows: list[tuple[str, dict, float, float]],
    top_k: int,
) -> list[tuple[str, dict, float, float]]:
    """
    LLM-based reranker: send up to 20 candidate passages to the LLM in a single
    prompt and ask it to return the indices of the most relevant ones in order.

    Falls back silently to the RRF-ranked order if the LLM is unavailable or
    returns unparseable output.  The distance field in each row is preserved so
    that the UI can still show a relevance percentage.
    """
    if len(rows) <= top_k:
        return rows

    candidates = rows[:30]  # cap at 30; larger pools improve reranker accuracy
    lines = [
        f"[{i + 1}] {doc[:350].replace(chr(10), ' ')}"
        for i, (doc, _, _, _) in enumerate(candidates)
    ]
    prompt = (
        f"Query: {query}\n\n"
        f"Below are {len(candidates)} document excerpts. "
        f"Return the numbers of the {top_k} most relevant excerpts for answering the query, "
        f"ranked from most to least relevant, separated by commas. "
        f"Output ONLY the numbers, nothing else. "
        f"If fewer than {top_k} excerpts are relevant, return as many as are.\n\n"
        + "\n".join(lines)
    )

    try:
        # A3-L2 (debt). Reranker timeout trimmed from 45s → 30s.
        raw = _llm_generate(prompt, max_tokens=60, temperature=0.0, timeout=30)
        import re as _re
        indices = [int(x) - 1 for x in _re.findall(r"\b(\d+)\b", raw)]
        # Deduplicate and keep only valid indices
        seen: set[int] = set()
        ordered: list[int] = []
        for idx in indices:
            if 0 <= idx < len(candidates) and idx not in seen:
                seen.add(idx)
                ordered.append(idx)
        if ordered:
            # Accept partial reranking: fill remaining slots from RRF order if short
            if len(ordered) < top_k:
                for idx in range(len(candidates)):
                    if idx not in seen:
                        ordered.append(idx)
                        seen.add(idx)
                    if len(ordered) >= top_k:
                        break
            # Audit round 4 — A3-L3. Don't log raw user queries (RGPD).
            logger.debug(
                "Reranker selected indices %s (query_len=%d)",
                ordered[:top_k], len(query),
            )
            return [candidates[i] for i in ordered[:top_k]]
    except Exception as exc:
        logger.debug("Reranker failed, falling back to RRF order: %s", exc)

    return rows[:top_k]


def _build_source(i: int, doc: str, meta: dict, dist: float) -> tuple[str, dict]:
    """Return (context_part, source_dict) for one result row."""
    source_type = meta.get("source_type", "newsletter")
    relevance = round((1 - dist) * 100)

    if source_type == "library":
        ref = f"[{i}] {meta.get('display_title', meta.get('filename', ''))}"
        src = {
            "num": i, "source_type": "library",
            "display_title": meta.get("display_title", meta.get("filename", "")),
            "filename": meta.get("filename", ""),
            "relative_path": meta.get("relative_path", ""),
            "tag": meta.get("tag", ""), "relevance": relevance,
        }
    elif source_type == "staged_article":
        ref = f"[{i}] {meta.get('source', 'Source inconnue')}. {meta.get('titre', '')}."
        if meta.get("lien"):
            ref += f" Disponible sur : {meta['lien']}"
        src = {
            "num": i, "source_type": "staged_article",
            "titre": meta.get("titre", ""), "source": meta.get("source", ""),
            "lien": meta.get("lien", ""), "category": meta.get("category", ""),
            "tag": meta.get("tag", ""), "relevance": relevance,
        }
    elif source_type in ("user_document", "cloud_document", "org_document"):
        title = meta.get("display_title") or meta.get("filename") or "Document"
        web_url = meta.get("web_url") or ""
        ref = f"[{i}] {title}."
        if web_url:
            ref += f" Disponible sur : {web_url}"
        src = {
            "num": i, "source_type": source_type,
            "display_title": title,
            "relevance": relevance,
            "cloud_doc_id": meta.get("cloud_doc_id"),
            "user_doc_id": meta.get("user_doc_id"),
            "web_url": web_url,
        }
    elif source_type == "user_scraper_article":
        source_name = meta.get("source_name", "Source RSS")
        title = meta.get("article_title", "")
        url = meta.get("article_url", "")
        ref = f"[{i}] {source_name}. {title}."
        if url:
            ref += f" Disponible sur : {url}"
        src = {
            "num": i, "source_type": "user_scraper_article",
            "source_name": source_name,
            "article_title": title,
            "article_url": url,
            "published_at": meta.get("published_at", ""),
            "relevance": relevance,
        }
    else:
        year = meta.get("newsletter_date", "")[:4] if meta.get("newsletter_date") else ""
        ref = f"[{i}] Myeline. {meta.get('section_title', '')}. Newsletter Myeline. {year}."
        src = {
            "num": i, "source_type": "newsletter",
            "section_title": meta.get("section_title", ""),
            "newsletter_title": meta.get("newsletter_title", ""),
            "date": meta.get("newsletter_date", ""),
            "tag": meta.get("tag", ""), "newsletter_id": meta.get("newsletter_id"),
            "relevance": relevance,
        }

    src["excerpt"] = doc[:400].strip()
    # Surface the canonical document date so the UI can render a per-source
    # date badge and compute a global freshness summary.
    if meta.get("doc_date"):
        src["doc_date"] = meta["doc_date"]
        src["doc_ts"] = meta.get("doc_ts")
    return f"--- Source {i} ---\n{ref}\n{doc[:1500]}", src


def _search_cache_key(query: str, scope: str, user_id, tag_filter,
                      date_from=None, date_to=None, strict_mode=True,
                      document_scope=None) -> str:
    import hashlib
    raw = (
        f"{query}|{scope}|{user_id}|{tag_filter}|{date_from}|{date_to}|"
        f"{int(bool(strict_mode))}|{document_scope or ''}"
    )
    return f"rag:{hashlib.sha256(raw.encode()).hexdigest()[:20]}"


def _freshness_summary(sources: list[dict]) -> Optional[dict]:
    """Compute newest / oldest / median ages from the doc_ts of cited sources.

    Returned dict (or None when no source carries a date):
      newest_iso, oldest_iso       — YYYY-MM-DD
      newest_age_days, median_age_days
      coverage                     — fraction of sources with a date (0..1)
    """
    dated = [s for s in sources if s.get("doc_ts")]
    if not dated:
        return None
    now = datetime.now(timezone.utc).timestamp()
    ages_days = sorted(max(0, int((now - int(s["doc_ts"])) // 86_400)) for s in dated)
    median = ages_days[len(ages_days) // 2]
    newest = min(dated, key=lambda s: now - int(s["doc_ts"]))
    oldest = max(dated, key=lambda s: now - int(s["doc_ts"]))
    return {
        "newest_iso": newest.get("doc_date", ""),
        "oldest_iso": oldest.get("doc_date", ""),
        "newest_age_days": ages_days[0],
        "median_age_days": median,
        "coverage": round(len(dated) / max(1, len(sources)), 2),
    }


def _get_cache():
    """Return a Redis client if caching is enabled and Redis is reachable, else None."""
    if _CACHE_TTL <= 0:
        return None
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        return None
    try:
        from redis import Redis
        return Redis.from_url(redis_url, socket_connect_timeout=1)
    except Exception:
        return None


def _recency_factor(meta: dict) -> float:
    """Exponential decay for time-stamped article/scraper content only.

    Returns 1.0 for personal documents and org content (timeless).
    Decays to 0.5 at _RECENCY_HALF_LIFE_DAYS for scraped articles and staged articles.
    """
    import math
    if meta.get("source_type") not in ("user_scraper_article", "staged_article"):
        return 1.0
    ts = meta.get("published_at") or meta.get("indexed_at", "")
    if not ts:
        return 1.0
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days = max(0, (datetime.now(timezone.utc) - dt).days)
        return math.exp(-days * math.log(2) / _RECENCY_HALF_LIFE_DAYS)
    except Exception:
        return 1.0


def _generate_query_variants(query: str, n: int = 2) -> list[str]:
    """Generate n alternative reformulations of a query for multi-query retrieval.

    Falls back to [query] on any error so the caller always gets at least the original.

    Cached in Redis (1 h TTL) keyed on (n, sha1(query)). The variant LLM
    call costs ~1-2 s on Mistral and ~3-8 s on local Ollama, every cache
    hit cuts that off the hot path entirely. Same caveat as the embed
    cache: failures fall through to a real LLM call.
    """
    import hashlib as _h
    import json as _json

    cache = _get_cache() if _EMBED_CACHE_TTL > 0 else None
    cache_key = (
        f"qvariants:{n}:{_h.sha1(query.encode('utf-8'), usedforsecurity=False).hexdigest()}"
        if cache else None
    )
    if cache and cache_key:
        try:
            hit = cache.get(cache_key)
            if hit:
                cached = _json.loads(hit)
                if isinstance(cached, list) and cached:
                    return cached
        except Exception as exc:
            logger.debug("variants cache read miss: %s", exc)

    prompt = (
        f"Generate exactly {n} alternative reformulations of the following search query, "
        "using synonyms or different angles. "
        "Reply in the same language as the query. "
        "Output only the reformulations, one per line, no numbering.\n\n"
        f"Query: {query}"
    )
    try:
        # A3-L2 (debt). Query-variant timeout trimmed from 15s → 10s.
        response = _llm_generate(prompt, max_tokens=80, temperature=0.4, timeout=10)
        lines = [line.strip() for line in response.splitlines() if line.strip()]
        out = [query] + lines[:n]
    except Exception as exc:
        logger.debug("Query variant generation failed: %s", exc)
        return [query]

    if cache and cache_key:
        try:
            cache.set(cache_key, _json.dumps(out), ex=_EMBED_CACHE_TTL)
        except Exception as exc:
            logger.debug("variants cache write failed: %s", exc)

    return out


def _effective_top_k(query: str) -> int:
    """Return a higher TOP_K for complex synthesis queries.

    Simple factual questions (short, single entity) get the base TOP_K.
    Complex synthesis requests (multi-part, comparison, enumeration) get +4.
    """
    words = query.lower().split()
    if len(words) > 15 or any(w in _SYNTHESIS_KEYWORDS for w in words):
        return min(_TOP_K + 4, 12)
    return _TOP_K


def search(
    query: str,
    tag_filter: Optional[str] = None,
    top_k: int = 0,  # 0 = auto (computed by _effective_top_k)
    scope: str = "shared",
    user_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    strict_mode: bool = True,
    history: Optional[list] = None,
    document_scope: Optional[str] = None,
    on_stage: Optional[Callable[[str], None]] = None,
    synthesis: bool = True,
) -> dict:
    """
    Semantic search + local LLM synthesis.

    scope:
      "shared"   — shared collection (newsletters + admin library), default
      "personal" — cloud documents only (source_type=cloud_document, requires user_id)
      "scrapers" — personal RSS/scraper articles only (source_type=user_scraper_article)
      "both"     — shared + personal (legacy alias for "all")
      "all"      — shared + personal cloud docs + scraper articles

    date_from / date_to: optional ISO YYYY-MM-DD strings. When set, only chunks
    whose `doc_ts` falls in [from, to] are retrieved. Chunks indexed before the
    feature shipped lack `doc_ts` and are excluded when a filter is active.

    on_stage is an optional progress callback invoked with the current
    pipeline stage ("embed" → "retrieve" → "rerank" → "synth"). It must
    never raise — failures are swallowed so a stuck observer can't break
    a search.
    """
    import hashlib as _hashlib
    import json as _json

    def _stage(name: str) -> None:
        if on_stage is None:
            return
        try:
            on_stage(name)
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("on_stage(%s) raised, ignoring: %s", name, exc)

    if top_k == 0:
        top_k = _effective_top_k(query)

    # ── Temporal filter — convert ISO dates to unix seconds for Chroma $gte/$lte ──
    date_filter: Optional[dict] = None
    ts_from = ts_to = None
    if date_from:
        try:
            ts_from = int(datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc).timestamp())
        except Exception:
            ts_from = None
    if date_to:
        try:
            # End-of-day so a YYYY-MM-DD upper bound includes the whole day.
            dt_to = datetime.fromisoformat(date_to).replace(tzinfo=timezone.utc)
            ts_to = int(dt_to.timestamp()) + 86_399
        except Exception:
            ts_to = None
    # Chroma rejects `{field: {"$gte": x, "$lte": y}}` — each operator must
    # live in its own clause, joined with $and.
    if ts_from is not None and ts_to is not None:
        date_filter = {"$and": [{"doc_ts": {"$gte": ts_from}}, {"doc_ts": {"$lte": ts_to}}]}
    elif ts_from is not None:
        date_filter = {"doc_ts": {"$gte": ts_from}}
    elif ts_to is not None:
        date_filter = {"doc_ts": {"$lte": ts_to}}

    # ── Cache lookup ──────────────────────────────────────────────────────────
    cache = _get_cache()
    cache_key = _search_cache_key(
        query, scope, user_id, tag_filter, date_from, date_to, strict_mode,
        document_scope,
    ) if cache else None
    if cache and cache_key:
        try:
            hit = cache.get(cache_key)
            if hit:
                logger.debug("RAG cache hit for key %s", cache_key)
                return _json.loads(hit)
        except Exception as exc:
            logger.debug("RAG cache read error: %s", exc)

    # €0-on-Free: HyDE, multi-query variants and (later) reranking all call the
    # platform LLM (Mistral). They are SKIPPED for Free individual users — search
    # still works (hybrid vector+keyword + RRF/MMR), just without the augmented
    # passes, which become a Pro value-add. Pro/Enterprise/admin keep everything.
    allow_aux = _platform_embed_allowed(user_id)

    _stage("embed")
    try:
        # HyDE adds value for conversational/question queries; skip for short keyword lookups
        # (and entirely for Free — it costs a platform LLM call).
        if allow_aux and _should_use_hyde(query):
            query_embedding = _hyde_embed(query)
        else:
            query_embedding = _embed_query(query)
    except requests.RequestException as e:
        logger.error(f"Ollama indisponible (embedding): {e}")
        return {"answer": "Le moteur de recherche est temporairement indisponible.", "sources": []}

    # ── Multi-query expansion ─────────────────────────────────────────────────
    # Generate query variants only for non-trivial queries to avoid extra LLM calls
    # on simple keyword lookups. Variants are embedded separately and retrieved in parallel;
    # RRF scores accumulate across variants so chunks found by multiple reformulations
    # get a natural boost (same principle as cross-collection RRF fusion).
    if allow_aux and _MULTIQUERY_ENABLED and len(query.strip()) > 30:
        query_variants = _generate_query_variants(query, n=_MULTIQUERY_N)
        # Embed variants in parallel — each Ollama call is ~300-700ms HTTP-bound.
        # 2-3 variants × 500ms serial = 1-1.5s on the hot path → ~500ms parallel.
        try:
            from concurrent.futures import ThreadPoolExecutor
            extra = query_variants[1:]
            if extra:
                with ThreadPoolExecutor(max_workers=min(len(extra), 4)) as pool:
                    extra_embeddings = list(pool.map(_embed_query, extra))
            else:
                extra_embeddings = []
            variant_embeddings = [query_embedding] + extra_embeddings
        except Exception as exc:
            logger.debug("Variant embedding failed, using single query: %s", exc)
            query_variants = [query]
            variant_embeddings = [query_embedding]
    else:
        query_variants = [query]
        variant_embeddings = [query_embedding]

    # When a user has BYOK embedding, their user_{id}_personal collection lives
    # in a different vector space than the platform-default `shared` library.
    # Query embeddings for those calls must be computed with the user's
    # provider, lazily and cached so multiple retrievals against the same
    # collection don't repeat the API call.
    _default_sig = (_active_embed_provider(), _active_embed_model())
    _qembs_by_sig: dict = {_default_sig: variant_embeddings}

    def _resolve_qembs(col_user_id=None):
        # A collection's vectors live in the embed space of whoever owns it:
        # the user's BYOK embed model (user_{id}_personal) or the platform
        # default (shared library). The query variants must be re-embedded in
        # that same space, lazily + cached.
        if col_user_id is None:
            return variant_embeddings
        sig = (
            _active_embed_provider(col_user_id),
            _active_embed_model(col_user_id),
        )
        if sig == _default_sig:
            return variant_embeddings
        cached = _qembs_by_sig.get(sig)
        if cached is not None:
            return cached
        try:
            embs = [_embed_query(q, user_id=col_user_id) for q in query_variants]
        except EmbedKeyRequiredError:
            # Free user with no embed key — their personal collection is empty
            # anyway (they can't have indexed), so an empty/default embed yields
            # no matches. Don't crash the whole search.
            embs = variant_embeddings
        except Exception as exc:
            logger.warning(
                "BYOK query embed (user=%s) failed (%s); using default embed",
                col_user_id, exc,
            )
            embs = variant_embeddings
        _qembs_by_sig[sig] = embs
        return embs

    tag_where = {"tag": tag_filter} if tag_filter else None

    def _merge_where(*clauses) -> Optional[dict]:
        """Combine non-empty Chroma where-clauses with $and. Returns None if all empty."""
        active = [c for c in clauses if c]
        if not active:
            return None
        if len(active) == 1:
            return active[0]
        return {"$and": active}

    # (doc, meta, distance, rrf_score) — collected across all sub-collections and query variants
    rows: list[tuple[str, dict, float, float]] = []

    def _add(col, n, where=None, col_user_id=None):
        # Merge per-call where with the global date filter so the temporal
        # constraint applies to every sub-collection / source-type query.
        # ``col_user_id`` selects the matching embed space (BYOK user-personal
        # or platform default).
        effective_where = _merge_where(where, date_filter)
        qembs = _resolve_qembs(col_user_id)
        for q, qemb in zip(query_variants, qembs):
            d, m, dist, rff = _hybrid_query_collection(col, qemb, q, n, effective_where)
            rows.extend(zip(d, m, dist, rff))

    _stage("retrieve")

    # ── Single-document scope — overrides the regular scope wiring ────────
    if document_scope:
        try:
            kind, raw_id = document_scope.split(":", 1)
            doc_id_int = int(raw_id)
        except (ValueError, AttributeError):
            kind, doc_id_int = None, None
        if kind == "user_doc" and user_id is not None and doc_id_int:
            _add(_get_user_collection(user_id), _RETRIEVAL_K, {"user_doc_id": doc_id_int}, col_user_id=user_id)
        elif kind == "cloud_doc" and user_id is not None and doc_id_int:
            _add(_get_user_collection(user_id), _RETRIEVAL_K, {"cloud_doc_id": doc_id_int}, col_user_id=user_id)
        # Document-scoped search ignores the broader scope wiring below —
        # the user explicitly asked about ONE document, so we don't sneak
        # in chunks from elsewhere even if scope=all was passed.
    else:
        if scope in ("shared", "both"):
            _add(_get_collection(), _RETRIEVAL_K, tag_where)

        if scope in ("personal", "mine", "both", "all") and user_id is not None:
            personal_col = _get_user_collection(user_id)
            # Always query each personal source type separately so every category
            # gets _RETRIEVAL_K candidate slots, preventing any single large source
            # (e.g. 150 scraper chunks) from crowding out personal documents.
            for stype in ("user_document", "cloud_document"):
                _add(personal_col, _RETRIEVAL_K, {"source_type": stype}, col_user_id=user_id)

        if scope in ("scrapers", "mine", "all") and user_id is not None:
            personal_col = _get_user_collection(user_id)
            _add(personal_col, _RETRIEVAL_K, {"source_type": "user_scraper_article"}, col_user_id=user_id)

    # Content shared with the user by other users (documents + scraper articles).
    # Skipped under document_scope — single-doc chat must stay scoped.
    if not document_scope and scope in ("shared_docs", "all") and user_id is not None:
        try:
            from app.models.sharing import DocumentShare, ScraperShare
            for share in ScraperShare.query.filter_by(shared_with_id=user_id).all():
                col = _get_user_collection(share.owner_id)
                _add(col, _RETRIEVAL_K, where={"scraper_id": share.scraper_id}, col_user_id=share.owner_id)
            for share in DocumentShare.query.filter_by(shared_with_id=user_id).all():
                col = _get_user_collection(share.owner_id)
                _add(col, _RETRIEVAL_K, where={"user_doc_id": share.doc_id}, col_user_id=share.owner_id)
        except Exception as e:
            logger.error(f"Error querying shared content for user {user_id}: {e}")

    # Deduplicate by chunk hash, accumulating RRF scores across multiple retrievals.
    # A chunk found by the dense pass, the sparse pass, AND a query variant accumulates
    # all three contributions — this is the cross-retrieval RRF fusion signal.
    # The semantic distance is kept as the minimum (best cosine similarity seen).
    seen_ids: dict[str, int] = {}
    deduped: list[list] = []  # mutable so we can update scores in-place
    for row in rows:
        key = _hashlib.sha256(row[0][:300].encode(), usedforsecurity=False).hexdigest()[:16]
        if key in seen_ids:
            ex = deduped[seen_ids[key]]
            ex[2] = min(ex[2], row[2])   # best (lowest) cosine distance
            ex[3] += row[3]              # accumulate RRF score
        else:
            seen_ids[key] = len(deduped)
            deduped.append(list(row))
    rows = [tuple(r) for r in deduped]

    # Optional recency decay for article/scraper content
    if _RECENCY_BOOST:
        rows = [
            (doc, meta, dist, rrf * _recency_factor(meta))
            for doc, meta, dist, rrf in rows
        ]

    if not rows:
        if scope == "personal":
            return {
                "answer": "Vous n'avez pas encore de documents indexés dans votre bibliothèque personnelle.",
                "sources": [],
            }
        tag_msg = f" sur le thème « {tag_filter} »" if tag_filter else ""
        return {
            "answer": f"Aucun résultat pertinent trouvé{tag_msg}.",
            "sources": [],
        }

    # Sort by RRF score (descending = most relevant first)
    rows.sort(key=lambda x: x[3], reverse=True)

    # LLM reranker (optional): evaluates relevance of the top candidates and reorders.
    # We give it top_k*2 slots so MMR has a larger pool to diversify from.
    if allow_aux and _RERANK_ENABLED:
        _stage("rerank")
        rerank_pool = min(len(rows), top_k * 2)
        rows = _rerank(query, rows, rerank_pool)

    # MMR: pick top_k results that balance relevance and diversity, removing
    # near-duplicate chunks (same document retrieved by multiple sub-queries).
    rows = _mmr_select(rows, top_k)

    context_parts = []
    sources = []
    for i, (doc, meta, dist, _rrf) in enumerate(rows, 1):
        ctx, src = _build_source(i, doc, meta, dist)
        # Flag content that belongs to another user (received share)
        if user_id and meta.get("user_id") and int(meta["user_id"]) != user_id:
            src["is_shared"] = True
        context_parts.append(ctx)
        sources.append(src)

    context = "\n\n".join(context_parts)

    # Multi-turn: inline the prior turns as a preamble so the LLM keeps
    # continuity (pronouns, follow-ups). We cap at the last 6 turns
    # (≈ 3 user/assistant pairs) and truncate each to 600 chars to keep
    # the synthesis context window tight — Mistral medium has 32k tokens
    # but the auxiliary calls share the same budget. Retrieval is still
    # driven by the *current* query alone, not the history, so the source
    # set isn't polluted by stale topics.
    history_preamble = ""
    if history:
        last = history[-6:]
        lines = []
        for turn in last:
            role = turn.get("role", "user")
            content = (turn.get("content") or "")[:600]
            lines.append(f"{role.capitalize()}: {content}")
        history_preamble = (
            "Conversation précédente (à utiliser uniquement pour la continuité, "
            "pas pour citer) :\n" + "\n".join(lines) + "\n\n"
        )
    user_msg = f"{history_preamble}Question : {query}\n\nExtraits de documents :\n{context}"
    tok = _max_tokens(query)

    # Synthesis is the most expensive step (5-30 s on local Ollama, ~1-3 s
    # on Mistral cloud). Some callers — watch-alerts cron, similarity-only
    # fan-out — only need the source list and discard the answer. They
    # pass synthesis=False to skip the LLM call entirely.
    answer = ""
    ai_warning = None
    if synthesis:
        _stage("synth")
        try:
            answer, ai_warning = _chat(user_msg, user_id=user_id, max_tokens=tok, strict_mode=strict_mode)
        except requests.RequestException as e:
            logger.error(f"LLM unavailable (synthesis): {e}")
            answer = "Le moteur de synthèse est temporairement indisponible."
            ai_warning = None

    result: dict = {"answer": answer, "sources": sources}
    if ai_warning:
        result["ai_warning"] = ai_warning
    freshness = _freshness_summary(sources)
    if freshness:
        result["freshness"] = freshness

    # ── Cache store ───────────────────────────────────────────────────────────
    if cache and cache_key:
        try:
            # Shorter TTL for personal/org scopes where content changes more frequently.
            ttl = _CACHE_TTL if scope == "shared" else max(60, _CACHE_TTL // 2)
            cache.set(cache_key, _json.dumps(result), ex=ttl)
        except Exception as exc:
            logger.debug("RAG cache write error: %s", exc)

    return result
