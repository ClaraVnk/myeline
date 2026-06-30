"""Shared indexing logic for all cloud storage connectors."""

import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from app.utils.logger import get_logger


logger = get_logger("cloud_sync_utils", subfolder="services")


# ---------------------------------------------------------------------------
# Token refresh lock (round 5 A2-H3)
# ---------------------------------------------------------------------------
#
# parallel_download_then_index() runs up to 4 threads against the same
# CloudConnection; each thread may notice an expired OAuth access_token
# and POST a refresh in parallel. Two concurrent refreshes can:
#   1. Burn the refresh_token (some IdPs rotate it per-grant), leaving
#      the second thread holding an invalid grant.
#   2. Race the encrypted-bytes write to conn.access_token_enc.
#
# This lock serialises refreshes per connection. Redis-down is treated
# as fail-safe — the lock simply degrades to a no-op so a transient
# Redis outage cannot stall cloud sync. The double-checked-locking
# pattern in each provider's _get_token() means the only loss is the
# race window itself, not correctness of subsequent calls.

_TOKEN_LOCK_KEY = "cloud_token_refresh:{cid}"
_TOKEN_LOCK_TTL = 30  # seconds — longer than a slow OAuth POST


@contextmanager
def cloud_token_refresh_lock(connection_id: int, wait_seconds: float = 10.0):
    """Per-connection lock for OAuth token refresh.

    Acquires via Redis SETNX with a 30 s TTL. If another worker holds
    the lock, polls (every 200 ms) until either it releases or
    ``wait_seconds`` is exceeded — at which point we yield anyway and
    let the caller's double-check decide what to do.
    """
    import redis

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    try:
        r = redis.from_url(redis_url, socket_connect_timeout=2.0, socket_timeout=2.0)
    except Exception as exc:
        logger.warning("cloud_token_refresh_lock: Redis unreachable, proceeding without lock: %s", exc)
        yield False
        return

    key = _TOKEN_LOCK_KEY.format(cid=connection_id)
    deadline = time.monotonic() + wait_seconds
    acquired = False
    try:
        while True:
            try:
                if r.set(key, "1", nx=True, ex=_TOKEN_LOCK_TTL):
                    acquired = True
                    break
            except Exception as exc:
                logger.warning("cloud_token_refresh_lock: Redis error, proceeding without lock: %s", exc)
                yield False
                return
            if time.monotonic() >= deadline:
                logger.warning(
                    "cloud_token_refresh_lock: timed out after %.1fs on connection_id=%s",
                    wait_seconds, connection_id,
                )
                break
            time.sleep(0.2)
        yield acquired
    finally:
        if acquired:
            try:
                r.delete(key)
            except Exception:
                # Lock will expire on TTL — not fatal.
                pass


def is_safe_web_url(url: Optional[str]) -> bool:
    """Round 5 A2-M5: refuse non-https schemes before storing provider URLs.

    Cloud providers always return https:// links in normal operation; a
    different scheme indicates a tampered API response or a
    misconstructed local URL. Letting it through means it ends up in
    the dashboard ``href`` (filtered by safeUrlJs front-side, but
    defense-in-depth) and in RAG reference text echoed by the LLM.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme == "https" and bool(parsed.netloc)

# Hard ceiling regardless of plan — even Enterprise (500 MB max_file_size in
# user_library_service._LIMITS) shouldn't hit this. Acts as a defense-in-depth
# cap; the plan-aware per-user check below is what matters in practice.
_HARD_MAX_BYTES = 1 * 1024 * 1024 * 1024  # 1 GB

_INDEXABLE: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".docx",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/vnd.oasis.opendocument.text": ".odt",
}

_EXT_TO_MIME: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".odt": "application/vnd.oasis.opendocument.text",
}


def mime_from_ext(filename: str) -> Optional[str]:
    ext = Path(filename).suffix.lower()
    return _EXT_TO_MIME.get(ext)


def is_indexable(mime: str) -> bool:
    return mime in _INDEXABLE


def has_failed_docs(connection_id: int) -> bool:
    """Return True if any document for this connection failed indexing (chunk_count=0).

    Used to force a full re-sync when the delta cursor would skip stuck documents.
    """
    from app.models.cloud_connection import CloudDocument
    return CloudDocument.query.filter_by(
        connection_id=connection_id, chunk_count=0
    ).count() > 0


# ---------------------------------------------------------------------------
# Sync progress reporting
# ---------------------------------------------------------------------------
#
# Each connector calls ``start_sync_progress`` once the file list is known,
# ``bump_sync_progress`` after every processed file (success, skip, or error),
# and ``clear_sync_progress`` in its ``finally`` block. The values are read
# back by the user-facing /user/cloud/sync-status endpoint to drive a banner
# on every dashboard page (see app/templates/base/_sync_banner.html).

def start_sync_progress(conn, total: int) -> None:
    """Mark the connection as actively syncing, with a known total.

    Called once at the start of a sync run. ``processed_files`` is reset
    to 0; call ``set_sync_total`` later to adjust the total without
    losing the running count (useful for paginated delta syncs).
    """
    from app.ext import db
    conn.sync_started_at = datetime.now(timezone.utc)
    conn.sync_total_files = max(0, int(total))
    conn.sync_processed_files = 0
    db.session.commit()


def set_sync_total(conn, total: int) -> None:
    """Update the running total without resetting processed_files.

    Used by paginated delta syncs that discover their work page by page.
    """
    from app.ext import db
    conn.sync_total_files = max(0, int(total))
    db.session.commit()


def bump_sync_progress(conn, n: int = 1) -> None:
    """Atomically increment the processed-files counter.

    Uses SQL ``UPDATE … SET col = col + n`` so concurrent worker threads
    (parallel downloads) don't lose updates. A round-trip per file is
    fine: 500 commits over a sync that includes 500 network downloads is
    insignificant compared to the I/O.
    """
    from sqlalchemy import update

    from app.ext import db
    from app.models.cloud_connection import CloudConnection
    if not getattr(conn, "id", None):
        return
    db.session.execute(
        update(CloudConnection)
        .where(CloudConnection.id == conn.id)
        .values(
            sync_processed_files=(
                db.func.coalesce(CloudConnection.sync_processed_files, 0) + n
            )
        )
    )
    db.session.commit()


def clear_sync_progress(conn) -> None:
    """Reset progress fields. Called in the connector's ``finally`` block
    alongside ``is_syncing = False`` so the banner disappears whether the
    sync succeeded or raised.
    """
    from app.ext import db
    conn.sync_started_at = None
    conn.sync_total_files = None
    conn.sync_processed_files = None
    db.session.commit()


def process_and_index(
    conn,
    remote_id: str,
    name: str,
    mime: str,
    size: int,
    modified_at: Optional[datetime],
    download_fn: Callable[[], bytes],
    ext: str,
    stats: dict,
    existing=None,
    preloaded_content: Optional[bytes] = None,
    web_url: Optional[str] = None,
) -> None:
    """Upsert CloudDocument and index file content. Mutates stats in-place.

    download_fn is called only if the file actually needs re-indexing.
    modified_at must be a naive UTC datetime or None.

    Bumps the connection's ``sync_processed_files`` counter exactly once
    per call (regardless of which exit path is taken) so the dashboard
    banner can report accurate progress.
    """
    from app.ext import db
    from app.models.cloud_connection import CloudDocument
    from app.models.user import User
    from app.services.rag_service import index_cloud_document, remove_cloud_document_from_index
    from app.services.user_library_service import get_limits
    from app.utils.time_utils import utcnow

    bump_sync_progress(conn)

    # Resolve plan-specific quotas (per-file size cap + total document count).
    # Falls back to the strictest tier if the user can't be loaded.
    user = db.session.get(User, conn.user_id)
    if user is None:
        from app.services.user_library_service import _LIMITS
        limits = _LIMITS["standard"]
    else:
        limits = get_limits(user)
    max_file_size = min(limits["max_file_size"], _HARD_MAX_BYTES)

    if size > max_file_size:
        logger.info(
            "Skipping %s: too large for plan (%d > %d bytes, user_id=%s)",
            name, size, max_file_size, conn.user_id,
        )
        stats["skipped"] += 1
        stats.setdefault("skipped_too_large", 0)
        stats["skipped_too_large"] += 1
        return

    # Resolve existing doc — callers may pass it directly (delta sync) or leave it
    # to us to look up (full sync). This avoids duplicate-key errors on full re-sync.
    if existing is None:
        existing = CloudDocument.query.filter_by(
            connection_id=conn.id, remote_id=remote_id
        ).first()

    if existing and existing.remote_modified_at == modified_at and existing.chunk_count > 0:
        stats["skipped"] += 1
        return

    # Plan-wide document cap (aggregate across every cloud connection of the
    # user). Only blocks NEW docs — re-indexing an existing one stays allowed
    # so a stuck document can be retried after a token refresh, etc.
    if existing is None:
        total_docs = CloudDocument.query.filter_by(user_id=conn.user_id).count()
        if total_docs >= limits["max_files"]:
            logger.info(
                "Skipping %s: plan quota reached (%d/%d, user_id=%s)",
                name, total_docs, limits["max_files"], conn.user_id,
            )
            stats["skipped"] += 1
            stats.setdefault("skipped_quota", 0)
            stats["skipped_quota"] += 1
            return

    if preloaded_content is not None:
        content = preloaded_content
    else:
        try:
            content = download_fn()
        except Exception as e:
            logger.error(f"Download failed for {name} ({remote_id}): {e}")
            stats["errors"] += 1
            return

    if existing:
        doc = existing
        if doc.chunk_count > 0:
            remove_cloud_document_from_index(conn.user_id, doc.id, doc.chunk_count)
    else:
        doc = CloudDocument(connection_id=conn.id, user_id=conn.user_id, remote_id=remote_id)
        db.session.add(doc)

    doc.name = name
    doc.mime_type = mime
    doc.file_size = size
    doc.remote_modified_at = modified_at
    if web_url is not None:
        # A2-M5: only accept https:// URLs from providers; anything else
        # ends up in the dashboard or the LLM reference text and could
        # smuggle javascript: or data: URLs through.
        doc.web_url = web_url if is_safe_web_url(web_url) else None
    db.session.flush()

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        n_chunks = index_cloud_document(doc.id, conn.user_id, tmp_path, name)
    finally:
        tmp_path.unlink(missing_ok=True)

    if n_chunks > 0:
        doc.chunk_count = n_chunks
        doc.indexed_at = utcnow()
        stats["indexed"] += 1
    else:
        doc.chunk_count = 0
        stats["skipped"] += 1

    db.session.commit()


def remove_cloud_doc(doc) -> None:
    from app.ext import db
    from app.services.rag_service import remove_cloud_document_from_index

    if doc.chunk_count > 0:
        remove_cloud_document_from_index(doc.user_id, doc.id, doc.chunk_count)
    db.session.delete(doc)
    db.session.commit()


_DOWNLOAD_CONCURRENCY = 4


def parallel_download_then_index(items: list, max_workers: int = _DOWNLOAD_CONCURRENCY) -> None:
    """Pre-download a batch of cloud-sync items in a thread pool, then call
    ``process_and_index`` sequentially with the bytes already in memory.

    Each item is a dict matching ``process_and_index`` kwargs; the
    ``download_fn`` is invoked once in the pool, the result fed back as
    ``preloaded_content``. Indexing stays single-threaded so we don't share
    SQLAlchemy sessions or ChromaDB collections across workers.

    Items that pass the early-skip checks (size, unchanged, quota) before
    reaching the download step waste a small amount of work on a pre-fetch
    they didn't need — acceptable trade-off vs. the 3-4× speedup on full
    syncs where most files do need downloading. Delta syncs typically have
    few items per batch so the wasted work is small.
    """
    from concurrent.futures import ThreadPoolExecutor

    if not items:
        return

    def _dl(item: dict):
        try:
            return (item, item["download_fn"](), None)
        except Exception as exc:
            return (item, None, exc)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(_dl, items))

    for item, content, exc in results:
        stats = item["stats"]
        if exc is not None:
            logger.error("Download failed for %s: %s", item.get("name"), exc)
            stats["errors"] += 1
            # process_and_index isn't called on this branch, so we have to
            # bump the progress counter ourselves to keep the banner accurate.
            try:
                bump_sync_progress(item["conn"])
            except Exception:
                logger.exception("Failed to bump sync progress on download error")
            continue
        try:
            process_and_index(**item, preloaded_content=content)
        except Exception as e:
            logger.error("Index failed for %s: %s", item.get("name"), e)
            stats["errors"] += 1
