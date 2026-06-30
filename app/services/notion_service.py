"""Notion connector via Notion Integration API.

Authentication: Notion Integration Token (Bearer).
Create an internal integration at https://www.notion.so/my-integrations,
then share pages/databases with it (Share → Invite → select integration).

Flow:
  1. notion_connect_form → user pastes the integration token
  2. store_credentials()  → encrypts it
  3. sync_connection()    → search all shared pages, extract block text, index
"""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests

from app.utils.logger import get_logger


logger = get_logger("notion_service", subfolder="services")

_API = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def store_credentials(conn, token: str) -> None:
    from app.utils.crypto import encrypt_token
    conn.access_token_enc = encrypt_token(token)
    conn.refresh_token_enc = None
    conn.token_expiry = None
    conn.sync_cursor = None


def _get_token(conn) -> str:
    from app.utils.crypto import decrypt_token
    return decrypt_token(conn.access_token_enc)


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }


def check_credentials(token: str) -> bool:
    """Return True if the token is valid."""
    try:
        resp = requests.get(f"{_API}/users/me", headers=_headers(token), timeout=10)
        return resp.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Text extraction from Notion blocks
# ---------------------------------------------------------------------------

def _extract_rich_text(rich_text_list: list) -> str:
    # A2-L4: Notion serves rich_text as plain_text but the field is user-
    # controlled; treat it as if it could contain HTML, parallel to the
    # Zotero A2-H1 fix. Strip tags at indexing so any RAG retrieval or
    # admin preview that forgets |escape doesn't surface a stored XSS.
    text = "".join(rt.get("plain_text", "") for rt in rich_text_list)
    if not text:
        return ""
    try:
        import bleach
        return bleach.clean(text, tags=[], attributes={}, strip=True)
    except ImportError:
        import html as html_module
        import re
        return html_module.unescape(re.sub(r"<[^>]+>", "", text))


def _extract_blocks(token: str, block_id: str, depth: int = 0) -> list[str]:
    """Recursively extract plain-text lines from Notion blocks (max depth 6)."""
    if depth > 6:
        return []

    parts: list[str] = []
    cursor = None
    while True:
        url = f"{_API}/blocks/{block_id}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        try:
            resp = requests.get(url, headers=_headers(token), timeout=30)
            if resp.status_code != 200:
                break
            data = resp.json()
        except Exception:
            break

        for block in data.get("results", []):
            btype = block.get("type", "")
            content = block.get(btype, {})
            rich_text = content.get("rich_text", [])
            text = _extract_rich_text(rich_text)
            if text.strip():
                parts.append(text)

            if block.get("has_children") and btype not in ("child_database", "synced_block"):
                parts.extend(_extract_blocks(token, block["id"], depth + 1))

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return parts


def _page_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            t = _extract_rich_text(prop.get("title", []))
            if t:
                return t
    return "Untitled"


def _page_to_text(token: str, page: dict) -> tuple[str, str]:
    """Return (title, full_text) for a Notion page."""
    title = _page_title(page)
    lines = _extract_blocks(token, page["id"])
    return title, "\n".join(lines)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync_connection(connection_id: int) -> dict:
    from app.ext import db
    from app.models.cloud_connection import CloudConnection, CloudDocument
    from app.services.rag_service import index_cloud_document, remove_cloud_document_from_index
    from app.utils.time_utils import utcnow

    conn = db.session.get(CloudConnection, connection_id)
    if not conn:
        return {"error": "Connection not found"}

    conn.is_syncing = True
    db.session.commit()

    token = _get_token(conn)
    stats = {"indexed": 0, "skipped": 0, "deleted": 0, "errors": 0}

    try:
        from app.services.cloud_sync_utils import bump_sync_progress, set_sync_total, start_sync_progress
        start_sync_progress(conn, 0)
        running_total = 0
        cursor = None
        while True:
            body: dict = {
                "filter": {"property": "object", "value": "page"},
                "page_size": 100,
            }
            if cursor:
                body["start_cursor"] = cursor
            resp = requests.post(f"{_API}/search", headers=_headers(token), json=body, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            page_results = data.get("results", [])
            running_total += len(page_results)
            set_sync_total(conn, running_total)
            for page in page_results:
                bump_sync_progress(conn)
                page_id = page["id"]
                last_edited_raw = page.get("last_edited_time")
                modified_at = None
                if last_edited_raw:
                    try:
                        modified_at = datetime.fromisoformat(
                            last_edited_raw.replace("Z", "+00:00")
                        ).replace(tzinfo=None)
                    except Exception:
                        pass

                existing = CloudDocument.query.filter_by(
                    connection_id=conn.id, remote_id=page_id
                ).first()

                if (existing and existing.chunk_count > 0
                        and modified_at and existing.remote_modified_at == modified_at):
                    stats["skipped"] += 1
                    continue

                try:
                    title, content = _page_to_text(token, page)
                except Exception as exc:
                    logger.warning("Notion page %s text extraction failed: %s", page_id, exc)
                    stats["errors"] += 1
                    continue

                if not content.strip():
                    stats["skipped"] += 1
                    continue

                with tempfile.NamedTemporaryFile(
                    suffix=".txt", mode="w", encoding="utf-8", delete=False
                ) as tmp:
                    tmp.write(content)
                    tmp_path = Path(tmp.name)

                try:
                    if existing:
                        doc = existing
                        if doc.chunk_count > 0:
                            remove_cloud_document_from_index(conn.user_id, doc.id, doc.chunk_count)
                    else:
                        doc = CloudDocument(
                            connection_id=conn.id,
                            user_id=conn.user_id,
                            remote_id=page_id,
                        )
                        db.session.add(doc)

                    doc.name = title
                    doc.mime_type = "text/plain"
                    doc.file_size = len(content.encode())
                    doc.remote_modified_at = modified_at
                    db.session.flush()

                    n_chunks = index_cloud_document(doc.id, conn.user_id, tmp_path, title)
                    if n_chunks > 0:
                        doc.chunk_count = n_chunks
                        doc.indexed_at = utcnow()
                        stats["indexed"] += 1
                    else:
                        doc.chunk_count = 0
                        stats["skipped"] += 1

                    db.session.commit()
                except Exception as exc:
                    db.session.rollback()
                    logger.error("Notion page %s indexing failed: %s", page_id, exc)
                    stats["errors"] += 1
                finally:
                    tmp_path.unlink(missing_ok=True)

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

        conn.last_sync_at = datetime.now(timezone.utc)
        db.session.commit()
        logger.info("Notion sync %s done: %s", connection_id, stats)

    except Exception:
        logger.exception("Notion sync %s failed", connection_id)
        stats["errors"] += 1
    finally:
        from app.services.cloud_sync_utils import clear_sync_progress
        conn.is_syncing = False
        clear_sync_progress(conn)

    return stats
