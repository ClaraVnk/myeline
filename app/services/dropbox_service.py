"""Dropbox connector via API v2.

OAuth2 flow:
  1. get_auth_url()    → Dropbox consent screen (offline access)
  2. store_tokens()    → trade code for tokens
  3. list_folders()    → let user pick a root path
  4. sync_connection() → full list_folder + delta via cursor

Requires env vars:
  DROPBOX_APP_KEY, DROPBOX_APP_SECRET
  DROPBOX_REDIRECT_URI (e.g. https://myeline.io/user/cloud/dropbox/callback)
"""

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import requests

from app.services.cloud_sync_utils import _EXT_TO_MIME, is_indexable, remove_cloud_doc
from app.utils.logger import get_logger


logger = get_logger("dropbox_service", subfolder="services")

_AUTH_URL = "https://www.dropbox.com/oauth2/authorize"
_TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
_API = "https://api.dropboxapi.com/2"
_CONTENT = "https://content.dropboxapi.com/2"


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------

def _cfg():
    from flask import current_app
    return (
        current_app.config["DROPBOX_APP_KEY"],
        current_app.config["DROPBOX_APP_SECRET"],
    )


def get_auth_url(redirect_uri: str) -> tuple[str, str]:
    app_key, _ = _cfg()
    state = secrets.token_urlsafe(32)
    params = {
        "client_id": app_key,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "token_access_type": "offline",  # required for refresh token
    }
    return f"{_AUTH_URL}?{urlencode(params)}", state


def store_tokens(conn, code: str, redirect_uri: str) -> None:
    from app.utils.crypto import encrypt_token

    app_key, app_secret = _cfg()
    resp = requests.post(
        _TOKEN_URL,
        data={
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        auth=(app_key, app_secret),
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
    """Return a valid Dropbox access token, refreshing if expired.

    A2-H3: double-checked locking serialises refreshes when 4 worker
    threads of ``parallel_download_then_index`` hit the same expired
    token in parallel.
    """
    from app.ext import db
    from app.services.cloud_sync_utils import cloud_token_refresh_lock
    from app.utils.crypto import decrypt_token, encrypt_token

    if _is_token_fresh(conn):
        return decrypt_token(conn.access_token_enc)

    if not conn.refresh_token_enc:
        return decrypt_token(conn.access_token_enc)

    with cloud_token_refresh_lock(conn.id):
        db.session.refresh(conn)
        if _is_token_fresh(conn):
            return decrypt_token(conn.access_token_enc)

        app_key, app_secret = _cfg()
        resp = requests.post(
            _TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": decrypt_token(conn.refresh_token_enc),
            },
            auth=(app_key, app_secret),
            timeout=15,
        )
        resp.raise_for_status()
        token = resp.json()
        conn.access_token_enc = encrypt_token(token["access_token"])
        if token.get("expires_in"):
            conn.token_expiry = (
                datetime.now(timezone.utc).replace(tzinfo=None)
                + timedelta(seconds=int(token["expires_in"]))
            )
        db.session.commit()
        return token["access_token"]


def _auth_header(conn) -> dict:
    return {"Authorization": f"Bearer {_get_token(conn)}"}


# ---------------------------------------------------------------------------
# Folder listing
# ---------------------------------------------------------------------------

def list_folders(conn) -> list[dict]:
    """List immediate subfolders of root (or of folder_id if set)."""
    root = conn.folder_id or ""
    resp = requests.post(
        f"{_API}/files/list_folder",
        headers=_auth_header(conn),
        json={"path": root, "recursive": False},
        timeout=15,
    )
    resp.raise_for_status()
    entries = resp.json().get("entries", [])
    return [
        {"id": e["path_lower"], "name": e["name"]}
        for e in entries
        if e.get(".tag") == "folder"
    ]


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
        logger.info(f"Dropbox sync {connection_id}: {stats}")
    except Exception as e:
        logger.exception(f"Dropbox sync {connection_id} failed: {e}")
        stats["errors"] += 1
    finally:
        from app.services.cloud_sync_utils import clear_sync_progress
        conn.is_syncing = False
        clear_sync_progress(conn)

    return stats


def _full_sync(conn, stats: dict) -> None:
    from app.ext import db

    root = conn.folder_id or ""
    h = _auth_header(conn)

    # Capture start cursor before listing so deltas are tracked from now on
    cursor_resp = requests.post(
        f"{_API}/files/list_folder/get_latest_cursor",
        headers=h,
        json={"path": root, "recursive": True, "include_deleted": True},
        timeout=15,
    )
    cursor_resp.raise_for_status()
    conn.sync_cursor = cursor_resp.json().get("cursor")

    # Full listing
    resp = requests.post(
        f"{_API}/files/list_folder",
        headers=h,
        json={"path": root, "recursive": True},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    from app.services.cloud_sync_utils import parallel_download_then_index, start_sync_progress

    batch = []
    while True:
        for entry in data.get("entries", []):
            if entry.get(".tag") != "file":
                continue
            descriptor = _build_index_descriptor(conn, entry, stats)
            if descriptor is not None:
                batch.append(descriptor)

        if not data.get("has_more"):
            break
        resp = requests.post(
            f"{_API}/files/list_folder/continue",
            headers=h,
            json={"cursor": data["cursor"]},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

    start_sync_progress(conn, len(batch))
    if batch:
        parallel_download_then_index(batch)

    db.session.commit()


def _delta_sync(conn, stats: dict) -> None:
    from app.ext import db
    from app.models.cloud_connection import CloudDocument

    h = _auth_header(conn)

    from app.services.cloud_sync_utils import parallel_download_then_index, set_sync_total, start_sync_progress

    # Total grows page by page; initialise the banner at 0 then bump as we go.
    start_sync_progress(conn, 0)
    running_total = 0
    while True:
        resp = requests.post(
            f"{_API}/files/list_folder/continue",
            headers=h,
            json={"cursor": conn.sync_cursor},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        batch = []
        # Pre-load all touched paths in one IN query — was 1 or 2 SELECTs
        # per entry depending on tag (perf audit H6).
        page_paths = [e.get("path_lower", "") for e in data.get("entries", []) if e.get("path_lower")]
        existing_map = {
            row.remote_id: row for row in CloudDocument.query.filter(
                CloudDocument.connection_id == conn.id,
                CloudDocument.remote_id.in_(page_paths),
            ).all()
        } if page_paths else {}

        for entry in data.get("entries", []):
            tag = entry.get(".tag")
            path = entry.get("path_lower", "")
            existing = existing_map.get(path)

            if tag == "deleted":
                if existing:
                    remove_cloud_doc(existing)
                    stats["deleted"] += 1
                continue

            if tag != "file":
                continue

            descriptor = _build_index_descriptor(conn, entry, stats, existing=existing)
            if descriptor is not None:
                batch.append(descriptor)

        # Bug M22 — l'ancien code avançait `sync_cursor` à chaque page
        # même quand parallel_download_then_index avait remonté des
        # erreurs (timeout, 5xx provider, indexation foirée). Les
        # documents loupés disparaissaient définitivement du delta sync.
        # Fix : ne pas avancer le cursor si la page a généré des erreurs
        # — le prochain tick reprend du même point et re-tente.
        errors_before = stats["errors"]
        if batch:
            running_total += len(batch)
            set_sync_total(conn, running_total)
            parallel_download_then_index(batch)

        if stats["errors"] == errors_before:
            conn.sync_cursor = data.get("cursor", conn.sync_cursor)
        else:
            logger.warning(
                "dropbox delta sync conn=%s page eut %d erreurs — sync_cursor non avancé",
                conn.id, stats["errors"] - errors_before,
            )
            break

        if not data.get("has_more"):
            break

    db.session.commit()


def _build_index_descriptor(conn, entry: dict, stats: dict, existing=None):
    """Build kwargs for ``parallel_download_then_index`` or None to skip."""
    path = entry.get("path_lower", "")
    name = entry.get("name", "unknown")

    # A2-L2: defense-in-depth on Dropbox-supplied paths. Dropbox itself
    # rejects ``..`` traversal server-side, but a future Dropbox policy
    # change shouldn't open us up to using the path as anything other
    # than an opaque remote_id. Refuse control chars + relative segments
    # outright.
    if (
        not path
        or path.startswith("../")
        or "/../" in path
        or any(ord(c) < 0x20 for c in path)
    ):
        logger.warning("Dropbox: refused suspicious path %r", path)
        return None

    mime = mime_from_path(name)
    if not mime:
        return None

    from app.services.cloud_sync_utils import _INDEXABLE as _IDX
    ext = _IDX[mime]
    size = int(entry.get("size", 0) or 0)

    modified_str = entry.get("server_modified") or entry.get("client_modified")
    modified_at = None
    if modified_str:
        try:
            modified_at = datetime.fromisoformat(
                modified_str.replace("Z", "+00:00")
            ).replace(tzinfo=None)
        except ValueError:
            pass

    token = _get_token(conn)

    def download() -> bytes:
        # Audit H5: serialise the API arg via json.dumps. Dropbox file
        # paths can contain `"` characters — an f-string injection
        # would either break the JSON header or, with a crafted
        # filename, smuggle additional fields ({"path":"x","mode":...}).
        import json as _json
        r = requests.post(
            f"{_CONTENT}/files/download",
            headers={
                "Authorization": f"Bearer {token}",
                "Dropbox-API-Arg": _json.dumps({"path": path}),
            },
            timeout=120,
        )
        r.raise_for_status()
        return r.content

    # Dropbox doesn't return a public URL in list_folder. The /preview/
    # form opens the in-app viewer for files the user owns.
    from urllib.parse import quote
    web_url = f"https://www.dropbox.com/home{quote(entry.get('path_display', path))}"

    return {
        "conn": conn,
        "remote_id": path,
        "name": name,
        "mime": mime,
        "size": size,
        "modified_at": modified_at,
        "download_fn": download,
        "ext": ext,
        "stats": stats,
        "existing": existing,
        "web_url": web_url,
    }


def mime_from_path(name: str) -> Optional[str]:
    ext = Path(name).suffix.lower()
    return _EXT_TO_MIME.get(ext)
