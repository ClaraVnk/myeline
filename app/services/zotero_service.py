"""Zotero connector via Zotero Web API v3.

Authentication: API key (not OAuth).
The user provides their Zotero API key + numeric user ID.

Indexing strategy:
  Zotero files are rarely synced to Zotero cloud storage (free quota: 300 MB).
  Rather than attempting file downloads that return 404, we index the
  bibliographic metadata of every item: title, abstract, authors, year,
  publication, tags.  This is usually richer for academic search than raw PDF
  text and requires no cloud storage subscription.

Data stored on CloudConnection:
  access_token_enc  → encrypted API key
  folder_id         → "<user_id>" or "<user_id>:<collection_key>"
  folder_name       → collection name or "All library"
  sync_cursor       → last seen library version number (string of int)
                       Previously stored an ISO timestamp — connections that
                       still have a timestamp cursor will fall back to full sync.
"""

from datetime import datetime, timezone
from typing import Optional

import requests

from app.utils.logger import get_logger


logger = get_logger("zotero_service", subfolder="services")

_API = "https://api.zotero.org"
_PAGE_SIZE = 100

# Item types that carry useful bibliographic metadata (exclude attachments/notes)
_SKIP_TYPES = {"attachment", "note", "annotation"}


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def store_credentials(conn, api_key: str, user_id: str) -> None:
    from app.utils.crypto import encrypt_token
    conn.access_token_enc = encrypt_token(api_key)
    conn.folder_id = user_id
    conn.folder_name = None
    conn.sync_cursor = None


def _get_api_key(conn) -> str:
    from app.utils.crypto import decrypt_token
    return decrypt_token(conn.access_token_enc)


def _parse_folder_id(folder_id: Optional[str]) -> tuple:
    if not folder_id:
        return "", None
    if ":" in folder_id:
        uid, col = folder_id.split(":", 1)
        return uid, col
    return folder_id, None


def _headers(conn) -> dict:
    return {
        "Zotero-API-Key": _get_api_key(conn),
        "Zotero-API-Version": "3",
    }


def _is_version_cursor(cursor: Optional[str]) -> bool:
    """Return True if cursor is a numeric library version (not an old ISO timestamp)."""
    if not cursor:
        return False
    try:
        int(cursor)
        return True
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Collections (exposed as "folders" to the generic UI)
# ---------------------------------------------------------------------------

def list_folders(conn) -> list:
    user_id, _ = _parse_folder_id(conn.folder_id)
    if not user_id:
        return []
    resp = requests.get(
        f"{_API}/users/{user_id}/collections",
        headers=_headers(conn),
        params={"limit": 100},
        timeout=15,
    )
    resp.raise_for_status()
    return [
        {"id": f"{user_id}:{item['key']}", "name": item["data"]["name"]}
        for item in resp.json()
    ]


# ---------------------------------------------------------------------------
# Sync entry point
# ---------------------------------------------------------------------------

def sync_connection(connection_id: int) -> dict:
    from app.ext import db
    from app.models.cloud_connection import CloudConnection

    conn = db.session.get(CloudConnection, connection_id)
    if not conn:
        return {"error": "Connection not found"}

    conn.is_syncing = True
    db.session.commit()

    stats = {"indexed": 0, "skipped": 0, "deleted": 0, "errors": 0}
    try:
        from app.services.cloud_sync_utils import has_failed_docs
        if _is_version_cursor(conn.sync_cursor) and not has_failed_docs(conn.id):
            _delta_sync(conn, stats)
        else:
            conn.sync_cursor = None
            _full_sync(conn, stats)

        conn.last_sync_at = datetime.now(timezone.utc)
        db.session.commit()
        logger.info("Zotero sync %d: %s", connection_id, stats)
    except Exception as e:
        logger.exception("Zotero sync %d failed: %s", connection_id, e)
        stats["errors"] += 1
    finally:
        from app.services.cloud_sync_utils import clear_sync_progress
        conn.is_syncing = False
        clear_sync_progress(conn)

    return stats


# ---------------------------------------------------------------------------
# Full sync (first run or cursor reset)
# ---------------------------------------------------------------------------

def _full_sync(conn, stats: dict) -> None:
    from app.ext import db

    user_id, collection_key = _parse_folder_id(conn.folder_id)
    if not user_id:
        logger.error("Zotero sync: no user_id stored")
        return

    from app.services.cloud_sync_utils import start_sync_progress

    h = _headers(conn)
    items, library_version = _fetch_items(user_id, collection_key, h, since=None)
    logger.info("Zotero full sync: %d items for user %s (library v%s)", len(items), user_id, library_version)

    start_sync_progress(conn, len(items))
    for item in items:
        _process_item(conn, item, stats)

    conn.sync_cursor = str(library_version)
    db.session.commit()


# ---------------------------------------------------------------------------
# Delta sync — uses ?since=<version> + /deleted endpoint
# ---------------------------------------------------------------------------

def _delta_sync(conn, stats: dict) -> None:
    from app.ext import db
    from app.models.cloud_connection import CloudDocument

    user_id, collection_key = _parse_folder_id(conn.folder_id)
    h = _headers(conn)
    since = conn.sync_cursor  # numeric string

    # Fetch items modified since last known library version
    changed_items, library_version = _fetch_items(user_id, collection_key, h, since=since)

    if library_version == int(since):
        # Library version unchanged: nothing to do
        logger.debug("Zotero delta sync: library unchanged at v%s", since)
        return

    logger.info(
        "Zotero delta sync: %d changed items (v%s → v%s)",
        len(changed_items), since, library_version,
    )

    from app.services.cloud_sync_utils import start_sync_progress
    start_sync_progress(conn, len(changed_items))

    # Bug H6 (audit round 2) — même pattern que Dropbox/OneDrive/kDrive :
    # ne pas avancer le cursor si la passe a généré des erreurs, sinon
    # les items en échec sont définitivement perdus (next delta démarre
    # du nouveau library_version qui les couvre).
    errors_before = stats["errors"]

    for item in changed_items:
        _process_item(conn, item, stats)

    # Fetch and process deletions (always at user level — no collection-scoped endpoint)
    deleted_keys = _fetch_deleted_keys(user_id, h, since=since)
    for key in deleted_keys:
        doc = CloudDocument.query.filter_by(
            connection_id=conn.id, remote_id=key
        ).first()
        if doc:
            _remove_cloud_doc(doc)
            stats["deleted"] += 1

    if stats["errors"] == errors_before:
        conn.sync_cursor = str(library_version)
    else:
        logger.warning(
            "zotero delta sync conn=%s eut %d erreurs — sync_cursor non avancé",
            conn.id, stats["errors"] - errors_before,
        )
    db.session.commit()


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _fetch_items(
    user_id: str,
    collection_key: Optional[str],
    h: dict,
    since: Optional[str],
) -> tuple:
    """Fetch bibliographic items (excluding attachments/notes), paginated.

    Returns (items, library_version) where library_version is an int
    from the Last-Modified-Version response header.
    """
    results = []
    start = 0
    library_version = 0

    base = (
        f"{_API}/users/{user_id}/collections/{collection_key}/items"
        if collection_key
        else f"{_API}/users/{user_id}/items"
    )

    while True:
        params = {"format": "json", "limit": _PAGE_SIZE, "start": start}
        if since is not None:
            params["since"] = since

        resp = requests.get(base, headers=h, params=params, timeout=30)
        resp.raise_for_status()

        # Capture library version from first page (consistent across all pages
        # unless the library is modified mid-fetch, in which case we re-sync next run)
        if start == 0:
            try:
                library_version = int(resp.headers.get("Last-Modified-Version", 0))
            except (ValueError, TypeError):
                library_version = 0

        batch = resp.json()
        if not batch:
            break

        for item in batch:
            if item.get("data", {}).get("itemType", "") not in _SKIP_TYPES:
                results.append(item)

        total = int(resp.headers.get("Total-Results", 0))
        start += _PAGE_SIZE
        if start >= total:
            break

    return results, library_version


def _fetch_deleted_keys(user_id: str, h: dict, since: str) -> list:
    """Return list of item keys deleted from the library since *since* version."""
    try:
        resp = requests.get(
            f"{_API}/users/{user_id}/deleted",
            headers=h,
            params={"since": since},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("items", [])
    except Exception as e:
        logger.warning("Zotero: failed to fetch deleted items: %s", e)
        return []


# ---------------------------------------------------------------------------
# Item processing
# ---------------------------------------------------------------------------

def _strip_html(text: str) -> str:
    """Round 5 A2-H1: Zotero metadata fields are plain text by intent.

    A user-controlled Zotero item title like
    ``<img src=x onerror='alert(1)'>`` would otherwise be indexed
    verbatim, retrieved as a RAG chunk, and either echoed by the LLM
    or surfaced in the source-card preview where any future template
    that forgets ``|escape`` becomes a stored-XSS sink. Strip tags at
    the ingestion boundary and unescape entities so downstream text
    is unambiguously plain.
    """
    if not text:
        return ""
    import html as html_module
    try:
        import bleach
        return bleach.clean(text, tags=[], attributes={}, strip=True)
    except ImportError:
        import re
        stripped = re.sub(r"<[^>]+>", "", text)
        return html_module.unescape(stripped)


def _clean(value) -> str:
    return _strip_html(value.strip()) if isinstance(value, str) else ""


def _build_text(data: dict) -> str:
    """Build a plain-text document from Zotero item metadata."""
    parts = []

    title = _clean(data.get("title", ""))
    if title:
        parts.append(title)

    creators = data.get("creators", [])
    if creators:
        names = []
        for c in creators:
            if c.get("name"):
                names.append(_clean(c["name"]))
            else:
                fn = _clean(c.get("firstName", ""))
                ln = _clean(c.get("lastName", ""))
                names.append(f"{fn} {ln}".strip())
        parts.append("Authors: " + ", ".join(n for n in names if n))

    for field in ("date", "year", "publicationTitle", "journalAbbreviation",
                  "publisher", "place", "volume", "issue", "pages", "DOI", "ISBN", "url"):
        val = _clean(data.get(field, ""))
        if val:
            parts.append(f"{field}: {val}")

    abstract = _clean(data.get("abstractNote", ""))
    if abstract:
        parts.append(abstract)

    tags = [_clean(t.get("tag", "")) for t in data.get("tags", []) if t.get("tag")]
    tags = [t for t in tags if t]
    if tags:
        parts.append("Tags: " + ", ".join(tags))

    return "\n".join(parts)


def _download_attachment(user_id: str, key: str, api_key: str) -> Optional[bytes]:
    """Try to download the file from Zotero cloud. Returns None if not available."""
    try:
        r = requests.get(
            f"{_API}/users/{user_id}/items/{key}/file",
            headers={"Zotero-API-Key": api_key, "Zotero-API-Version": "3"},
            timeout=120,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.content
    except requests.HTTPError:
        return None


_SNAPSHOT_MIME = {"application/zip", "application/x-zip-compressed", "text/html"}


def _find_attachment(user_id: str, item_key: str, h: dict) -> Optional[dict]:
    """Return the best indexable child attachment: PDF first, then snapshot, then other."""
    from app.services.cloud_sync_utils import is_indexable
    try:
        r = requests.get(
            f"{_API}/users/{user_id}/items/{item_key}/children",
            headers=h,
            params={"format": "json", "itemType": "attachment", "limit": 10},
            timeout=15,
        )
        r.raise_for_status()
        pdf_att = None
        snapshot_att = None
        for child in r.json():
            data = child.get("data", {})
            ct = data.get("contentType", "")
            link_mode = data.get("linkMode", "")
            if is_indexable(ct):
                pdf_att = child
                break  # PDF wins immediately
            if ct in _SNAPSHOT_MIME or link_mode == "imported_url":
                if snapshot_att is None:
                    snapshot_att = child
        return pdf_att or snapshot_att
    except Exception:
        pass
    return None


def _remove_cloud_doc(doc) -> None:
    from app.ext import db
    from app.services.rag_service import remove_cloud_document_from_index

    if doc.chunk_count > 0:
        remove_cloud_document_from_index(doc.user_id, doc.id, doc.chunk_count)
    db.session.delete(doc)
    db.session.commit()


def _process_item(conn, item: dict, stats: dict) -> None:
    import tempfile
    from pathlib import Path

    from app.ext import db
    from app.models.cloud_connection import CloudDocument
    from app.services.cloud_sync_utils import _INDEXABLE, bump_sync_progress, is_indexable
    from app.services.rag_service import index_cloud_document, remove_cloud_document_from_index
    from app.utils.time_utils import utcnow

    bump_sync_progress(conn)

    data = item.get("data", {})
    key = item.get("key") or data.get("key")
    if not key:
        return

    title = data.get("title") or key
    remote_id = key
    api_key = _get_api_key(conn)
    user_id, _ = _parse_folder_id(conn.folder_id)
    h = {"Zotero-API-Key": api_key, "Zotero-API-Version": "3"}

    modified_str = data.get("dateModified") or data.get("dateAdded")
    modified_at = None
    if modified_str:
        try:
            modified_at = datetime.fromisoformat(
                modified_str.replace("Z", "+00:00")
            ).replace(tzinfo=None)
        except ValueError:
            pass

    existing = CloudDocument.query.filter_by(
        connection_id=conn.id, remote_id=remote_id
    ).first()

    if existing and existing.remote_modified_at == modified_at and existing.chunk_count > 0:
        stats["skipped"] += 1
        return

    # Try to get full PDF content from cloud storage.
    attachment = _find_attachment(user_id, key, h)
    content: Optional[bytes] = None
    suffix = ".txt"
    mime = "text/plain"

    if attachment:
        att_key = attachment.get("key") or attachment.get("data", {}).get("key")
        att_ct = attachment.get("data", {}).get("contentType", "")
        # Map content type to file extension: PDF/DOCX from _INDEXABLE, snapshot → .zip
        if att_ct in _SNAPSHOT_MIME:
            att_suffix = ".zip" if "zip" in att_ct else ".html"
        else:
            att_suffix = _INDEXABLE.get(att_ct, ".pdf")
        raw = _download_attachment(user_id, att_key, api_key)
        if raw:
            content = raw
            suffix = att_suffix
            mime = att_ct
        else:
            logger.debug(
                "Attachment not in Zotero cloud for item %s — falling back to metadata. "
                "Enable Zotero file sync to index full content.",
                key,
            )

    if existing:
        doc = existing
        if doc.chunk_count > 0:
            remove_cloud_document_from_index(conn.user_id, doc.id, doc.chunk_count)
    else:
        doc = CloudDocument(
            connection_id=conn.id,
            user_id=conn.user_id,
            remote_id=remote_id,
        )
        db.session.add(doc)

    # Public Zotero item URL: opens the bibliographic record (and any
    # snapshot/PDF preview Zotero stores). Falls back to the user's library
    # root when the user_id can't be parsed.
    web_url = (
        f"https://www.zotero.org/users/{user_id}/items/{key}"
        if user_id else None
    )

    if content:
        doc.name = title[:500]
        doc.mime_type = mime
        doc.file_size = len(content)
        doc.remote_modified_at = modified_at
        doc.web_url = web_url
        db.session.flush()

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
    else:
        text = _build_text(data)
        if not text.strip():
            stats["skipped"] += 1
            return
        doc.name = title[:500]
        doc.mime_type = "text/plain"
        doc.file_size = len(text.encode())
        doc.remote_modified_at = modified_at
        doc.web_url = web_url
        db.session.flush()

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w", encoding="utf-8") as tmp:
            tmp.write(text)
            tmp_path = Path(tmp.name)

    try:
        n_chunks = index_cloud_document(doc.id, conn.user_id, tmp_path, title)
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
