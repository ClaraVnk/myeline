"""Microsoft OneDrive connector via Graph API v1.0.

OAuth2 flow:
  1. get_auth_url()   → Microsoft consent screen
  2. store_tokens()   → trade code for tokens, store encrypted in DB
  3. list_folders()   → let user pick a root folder
  4. sync_connection() → full scan + delta via Graph delta API

Requires env vars:
  ONEDRIVE_CLIENT_ID, ONEDRIVE_CLIENT_SECRET
  ONEDRIVE_REDIRECT_URI (e.g. https://myeline.io/user/cloud/onedrive/callback)
"""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import requests

from app.services.cloud_sync_utils import _INDEXABLE, is_indexable, remove_cloud_doc
from app.utils.logger import get_logger


logger = get_logger("onedrive_service", subfolder="services")

_GRAPH = "https://graph.microsoft.com/v1.0"
_AUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
_SCOPES = "Files.Read offline_access"


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------

def _cfg():
    from flask import current_app
    return (
        current_app.config["ONEDRIVE_CLIENT_ID"],
        current_app.config["ONEDRIVE_CLIENT_SECRET"],
    )


def get_auth_url(redirect_uri: str) -> tuple[str, str]:
    client_id, _ = _cfg()
    state = secrets.token_urlsafe(32)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": _SCOPES,
        "state": state,
        "response_mode": "query",
    }
    return f"{_AUTH_URL}?{urlencode(params)}", state


def store_tokens(conn, code: str, redirect_uri: str) -> None:
    """Exchange code and persist encrypted tokens in conn (caller must commit)."""
    from app.utils.crypto import encrypt_token

    client_id, client_secret = _cfg()
    resp = requests.post(
        _TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json()
    conn.access_token_enc = encrypt_token(token["access_token"])
    conn.refresh_token_enc = encrypt_token(token["refresh_token"]) if token.get("refresh_token") else None
    if token.get("expires_in"):
        conn.token_expiry = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(seconds=int(token["expires_in"]))
        )


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

def _is_token_fresh(conn) -> bool:
    if not conn.token_expiry:
        return False
    expiry = conn.token_expiry.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < expiry - timedelta(seconds=60)


def _get_token(conn) -> str:
    """Return a valid access token, refreshing from DB if expired.

    A2-H3: when ``parallel_download_then_index`` runs 4 worker threads
    against the same connection, several can race here on expiry.
    Double-checked locking via a per-connection Redis lock serialises
    the refresh and avoids burning the rotating refresh_token.
    """
    from app.ext import db
    from app.services.cloud_sync_utils import cloud_token_refresh_lock
    from app.utils.crypto import decrypt_token, encrypt_token

    if _is_token_fresh(conn):
        return decrypt_token(conn.access_token_enc)

    if not conn.refresh_token_enc:
        return decrypt_token(conn.access_token_enc)

    with cloud_token_refresh_lock(conn.id):
        # Another worker may have refreshed while we waited for the lock.
        db.session.refresh(conn)
        if _is_token_fresh(conn):
            return decrypt_token(conn.access_token_enc)

        client_id, client_secret = _cfg()
        resp = requests.post(
            _TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": decrypt_token(conn.refresh_token_enc),
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        resp.raise_for_status()
        token = resp.json()
        conn.access_token_enc = encrypt_token(token["access_token"])
        if token.get("refresh_token"):
            conn.refresh_token_enc = encrypt_token(token["refresh_token"])
        if token.get("expires_in"):
            conn.token_expiry = (
                datetime.now(timezone.utc).replace(tzinfo=None)
                + timedelta(seconds=int(token["expires_in"]))
            )
        db.session.commit()
        return token["access_token"]


def _headers(conn) -> dict:
    return {"Authorization": f"Bearer {_get_token(conn)}"}


# ---------------------------------------------------------------------------
# Folder listing
# ---------------------------------------------------------------------------

def list_folders(conn) -> list[dict]:
    resp = requests.get(
        f"{_GRAPH}/me/drive/root/children",
        headers=_headers(conn),
        params={"$filter": "folder ne null", "$select": "id,name", "$top": "100"},
        timeout=15,
    )
    resp.raise_for_status()
    return [{"id": f["id"], "name": f["name"]} for f in resp.json().get("value", [])]


# ---------------------------------------------------------------------------
# Sync
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
        if conn.sync_cursor and not has_failed_docs(conn.id):
            _delta_sync(conn, stats)
        else:
            conn.sync_cursor = None
            _full_sync(conn, stats)

        conn.last_sync_at = datetime.now(timezone.utc)
        db.session.commit()
        logger.info(f"OneDrive sync {connection_id}: {stats}")
    except Exception as e:
        logger.exception(f"OneDrive sync {connection_id} failed: {e}")
        stats["errors"] += 1
    finally:
        from app.services.cloud_sync_utils import clear_sync_progress
        conn.is_syncing = False
        clear_sync_progress(conn)

    return stats


def _delta_base_url(conn) -> str:
    if conn.folder_id:
        return f"{_GRAPH}/me/drive/items/{conn.folder_id}/delta"
    return f"{_GRAPH}/me/drive/root/delta"


def _full_sync(conn, stats: dict) -> None:
    from app.ext import db

    h = _headers(conn)
    items = []
    url: Optional[str] = _delta_base_url(conn)

    while url:
        resp = requests.get(url, headers=h, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        items.extend(data.get("value", []))
        if "@odata.deltaLink" in data:
            conn.sync_cursor = data["@odata.deltaLink"]
            url = None
        else:
            url = data.get("@odata.nextLink")

    logger.info("OneDrive full sync: %d items fetched for conn %s", len(items), conn.id)
    batch = []
    for item in items:
        if item.get("deleted") or "file" not in item:
            continue
        descriptor = _build_index_descriptor(conn, item, stats)
        if descriptor is not None:
            batch.append(descriptor)

    from app.services.cloud_sync_utils import parallel_download_then_index, start_sync_progress
    start_sync_progress(conn, len(batch))
    if batch:
        parallel_download_then_index(batch)

    db.session.commit()


def _delta_sync(conn, stats: dict) -> None:
    from app.ext import db
    from app.models.cloud_connection import CloudDocument
    from app.services.cloud_sync_utils import parallel_download_then_index, set_sync_total, start_sync_progress

    h = _headers(conn)
    url: Optional[str] = conn.sync_cursor

    start_sync_progress(conn, 0)
    running_total = 0
    while url:
        resp = requests.get(url, headers=h, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        batch = []
        # Single IN query for all changed item_ids — was a SELECT per
        # change (perf audit H6). OneDrive delta pages can hold hundreds
        # of items; one batched SELECT replaces N round-trips.
        page_ids = [it.get("id") for it in data.get("value", []) if it.get("id")]
        existing_map = {
            row.remote_id: row for row in CloudDocument.query.filter(
                CloudDocument.connection_id == conn.id,
                CloudDocument.remote_id.in_(page_ids),
            ).all()
        } if page_ids else {}

        for item in data.get("value", []):
            item_id = item.get("id")
            existing = existing_map.get(item_id)

            if item.get("deleted"):
                if existing:
                    remove_cloud_doc(existing)
                    stats["deleted"] += 1
                continue

            if "file" not in item:
                continue

            descriptor = _build_index_descriptor(conn, item, stats, existing=existing)
            if descriptor is not None:
                batch.append(descriptor)

        # Bug M22 — idem Dropbox : sync_cursor avançait même en cas
        # d'erreurs sur la page. Fix : ne pas persister le deltaLink
        # si la page a eu des erreurs, et stopper la boucle pour que
        # le prochain run reprenne du même point.
        errors_before = stats["errors"]
        if batch:
            running_total += len(batch)
            set_sync_total(conn, running_total)
            parallel_download_then_index(batch)

        if stats["errors"] > errors_before:
            logger.warning(
                "onedrive delta sync conn=%s page eut %d erreurs — sync_cursor non avancé",
                conn.id, stats["errors"] - errors_before,
            )
            break

        if "@odata.deltaLink" in data:
            conn.sync_cursor = data["@odata.deltaLink"]
            url = None
        else:
            url = data.get("@odata.nextLink")

    db.session.commit()


def _build_index_descriptor(conn, item: dict, stats: dict, existing=None):
    """Return a kwargs dict for ``parallel_download_then_index`` or None to skip.

    Encapsulates MIME resolution and the download closure so the parallel
    helper can fan out HTTP fetches without knowing OneDrive specifics.
    """
    from app.services.cloud_sync_utils import mime_from_ext

    item_id = item["id"]
    name = item.get("name", "unknown")
    mime = (item.get("file") or {}).get("mimeType", "") or ""
    size = int(item.get("size", 0) or 0)

    if not is_indexable(mime):
        ext_mime = mime_from_ext(name)
        if ext_mime:
            logger.info("OneDrive: MIME fallback %s → %r (was %r)", name, ext_mime, mime)
        mime = ext_mime or mime
        if not is_indexable(mime):
            logger.info("OneDrive: skipping %s (mime=%r, not an indexable type)", name, mime)
            return None

    ext = _INDEXABLE[mime]
    modified_str = item.get("lastModifiedDateTime")
    modified_at = None
    if modified_str:
        try:
            modified_at = datetime.fromisoformat(
                modified_str.replace("Z", "+00:00")
            ).replace(tzinfo=None)
        except ValueError:
            pass

    def download() -> bytes:
        # Bug L1 (audit round 2) — l'ancien code capturait `token` au
        # moment de la construction du descriptor, gelant la valeur
        # pour toute la durée d'un sync long. Sur un full_sync avec
        # milliers d'items et token TTL 1h, les downloads en fin de
        # batch échouaient 401. _get_token cache en interne — refresh
        # automatique si expiré.
        fresh_token = _get_token(conn)
        r = requests.get(
            f"{_GRAPH}/me/drive/items/{item_id}/content",
            headers={"Authorization": f"Bearer {fresh_token}"},
            timeout=120,
        )
        r.raise_for_status()
        return r.content

    return {
        "conn": conn,
        "remote_id": item_id,
        "name": name,
        "mime": mime,
        "size": size,
        "modified_at": modified_at,
        "download_fn": download,
        "ext": ext,
        "stats": stats,
        "existing": existing,
        "web_url": item.get("webUrl"),
    }
