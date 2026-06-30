"""Generic WebDAV connector (Nextcloud, ownCloud, Seafile, any WebDAV server).

Authentication: WebDAV URL + username + password (Basic Auth).

For Nextcloud the WebDAV root is typically:
  https://your-server.example.com/remote.php/dav/files/{username}/

Flow:
  1. webdav_connect_form → user enters URL, username, password
  2. store_credentials()  → encrypts all three
  3. list_folders()       → PROPFIND to let user pick a root folder
  4. sync_connection()    → recursive PROPFIND + GET + index
"""

import json
from datetime import datetime, timezone

import requests

from app.services.cloud_sync_utils import _INDEXABLE, is_indexable, mime_from_ext, process_and_index
from app.utils.logger import get_logger
from app.utils.safe_http import safe_get, safe_request


logger = get_logger("webdav_service", subfolder="services")


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def store_credentials(conn, url: str, username: str, password: str) -> None:
    from app.utils.crypto import encrypt_token
    conn.access_token_enc = encrypt_token(
        json.dumps({"url": url.rstrip("/"), "username": username})
    )
    conn.refresh_token_enc = encrypt_token(password)
    conn.token_expiry = None
    conn.sync_cursor = None


def _get_credentials(conn) -> tuple[str, str, str]:
    """Return (base_url, username, password)."""
    from app.utils.crypto import decrypt_token
    data = json.loads(decrypt_token(conn.access_token_enc))
    return data["url"], data["username"], decrypt_token(conn.refresh_token_enc)


# ---------------------------------------------------------------------------
# WebDAV helpers
# ---------------------------------------------------------------------------

_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<propfind xmlns="DAV:">'
    "  <prop>"
    "    <displayname/>"
    "    <resourcetype/>"
    "    <getcontentlength/>"
    "    <getlastmodified/>"
    "    <getcontenttype/>"
    "  </prop>"
    "</propfind>"
)


def _propfind(auth: tuple, base_url: str, path: str, depth: int = 1) -> list[dict]:
    # Audit M4: defusedxml protects against billion-laughs / quadratic-
    # blowup entity attacks. Stdlib ElementTree disables external entity
    # resolution since 3.7.1 but does NOT block entity expansion bombs.
    from defusedxml.ElementTree import fromstring as et_fromstring

    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    # SSRF-safe : safe_request bloque IPs internes/meta + DNS rebind.
    # Sans ça, un user pouvait sonder http://169.254.169.254/, redis://,
    # mariadb://, etc. via le form WebDAV (bug C2).
    sess = requests.Session()
    sess.auth = auth
    resp = safe_request(
        "PROPFIND", url,
        headers={"Depth": str(depth), "Content-Type": "application/xml; charset=utf-8"},
        data=_PROPFIND_BODY,
        timeout=30,
        session=sess,
    )
    resp.raise_for_status()

    ns = {"d": "DAV:"}
    tree = et_fromstring(resp.content)
    resources = []
    for response in tree.findall("d:response", ns):
        href = (response.findtext("d:href", "", ns) or "").rstrip("/")
        props = response.find(".//d:prop", ns)
        if props is None:
            continue

        rt = props.find("d:resourcetype", ns)
        is_dir = rt is not None and rt.find("d:collection", ns) is not None
        name = props.findtext("d:displayname", "", ns) or href.split("/")[-1]
        size = int(props.findtext("d:getcontentlength", "0", ns) or 0)
        last_mod_str = props.findtext("d:getlastmodified", "", ns)
        content_type = props.findtext("d:getcontenttype", "", ns) or ""

        last_modified = None
        if last_mod_str:
            try:
                from email.utils import parsedate_to_datetime
                last_modified = parsedate_to_datetime(last_mod_str).replace(tzinfo=None)
            except Exception:
                pass

        resources.append({
            "href": href,
            "name": name,
            "is_dir": is_dir,
            "size": size,
            "last_modified": last_modified,
            "content_type": content_type,
        })

    return resources


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_credentials(url: str, username: str, password: str) -> str:
    """Return 'ok', 'bad_auth', or 'error'."""
    try:
        # SSRF-safe (cf. bug C2). On rejette tout IP interne / metadata
        # BEFORE d'envoyer les credentials → un attaquant ne peut plus
        # exfiltrer des creds via http://169.254.169.254 et consorts.
        from app.utils.safe_http import SSRFBlockedError
        sess = requests.Session()
        sess.auth = (username, password)
        try:
            resp = safe_request(
                "PROPFIND", url.rstrip("/") + "/",
                headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
                data=_PROPFIND_BODY,
                timeout=15,
                session=sess,
            )
        except SSRFBlockedError as exc:
            logger.warning("WebDAV check_credentials SSRF-blocked: %s", exc)
            return "error"
        if resp.status_code == 401:
            return "bad_auth"
        if resp.status_code in (207, 200):
            return "ok"
        return "error"
    except Exception as exc:
        logger.warning("WebDAV check_credentials error: %s", exc)
        return "error"


def list_folders(conn) -> list[dict]:
    base_url, username, password = _get_credentials(conn)
    root_path = conn.folder_id or "/"
    try:
        resources = _propfind((username, password), base_url, root_path, depth=1)
    except Exception as exc:
        logger.error("WebDAV list_folders failed: %s", exc)
        return []
    return [
        {"id": r["href"], "name": r["name"]}
        for r in resources
        if r["is_dir"] and r["href"].rstrip("/") != root_path.rstrip("/")
    ]


def _list_files_recursive(auth: tuple, base_url: str, root_path: str) -> list[dict]:
    results = []
    queue = [root_path.rstrip("/") or "/"]
    visited: set[str] = set()

    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        try:
            resources = _propfind(auth, base_url, current, depth=1)
        except Exception as exc:
            logger.warning("WebDAV PROPFIND failed for %s: %s", current, exc)
            continue

        for r in resources:
            href = r["href"].rstrip("/")
            if href == current.rstrip("/"):
                continue
            if r["is_dir"]:
                if href not in visited:
                    queue.append(href)
            else:
                mime = r["content_type"] or mime_from_ext(r["name"]) or ""
                if is_indexable(mime):
                    r["_mime"] = mime
                    results.append(r)

    logger.info("WebDAV BFS done: root=%s → %d indexable files", root_path, len(results))
    return results


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync_connection(connection_id: int) -> dict:
    from app.ext import db
    from app.models.cloud_connection import CloudConnection, CloudDocument

    conn = db.session.get(CloudConnection, connection_id)
    if not conn:
        return {"error": "Connection not found"}

    conn.is_syncing = True
    db.session.commit()

    stats = {"indexed": 0, "skipped": 0, "deleted": 0, "errors": 0}
    base_url, username, password = _get_credentials(conn)
    auth = (username, password)
    root_path = conn.folder_id or "/"

    try:
        try:
            last_sync = (
                datetime.fromisoformat(conn.sync_cursor).replace(tzinfo=None)
                if conn.sync_cursor
                else None
            )
        except (ValueError, TypeError):
            last_sync = None

        from app.services.cloud_sync_utils import start_sync_progress
        files = _list_files_recursive(auth, base_url, root_path)
        conn.sync_cursor = datetime.now(timezone.utc).isoformat()
        # process_and_index counts every call (success or skip) so we can
        # report a max equal to the resolved file list — that's what the
        # banner ETA divides against.
        start_sync_progress(conn, len(files))

        from app.services.cloud_sync_utils import bump_sync_progress
        for item in files:
            remote_id = item["href"]
            name = item["name"]
            mime = item.get("_mime") or item.get("content_type") or mime_from_ext(name) or ""
            if not is_indexable(mime):
                # Counted in `files` but not actually processed — keep the
                # banner total honest.
                bump_sync_progress(conn)
                continue

            ext = _INDEXABLE.get(mime, "")
            size = item.get("size", 0)
            modified_at = item.get("last_modified")

            existing = CloudDocument.query.filter_by(
                connection_id=conn.id, remote_id=remote_id
            ).first()

            if last_sync and existing and modified_at and existing.chunk_count > 0:
                if modified_at <= last_sync:
                    stats["skipped"] += 1
                    bump_sync_progress(conn)
                    continue

            def download(href=remote_id) -> bytes:
                full_url = base_url.rstrip("/") + href
                # SSRF-safe download — bg même URL famille que _propfind
                sess = requests.Session()
                sess.auth = auth
                resp = safe_get(full_url, timeout=120, session=sess)
                resp.raise_for_status()
                return resp.content

            process_and_index(conn, remote_id, name, mime, size, modified_at, download, ext, stats, existing)

        conn.last_sync_at = datetime.now(timezone.utc)
        db.session.commit()
        logger.info("WebDAV sync %s done: %s", connection_id, stats)

    except Exception:
        logger.exception("WebDAV sync %s failed", connection_id)
        stats["errors"] += 1
    finally:
        from app.services.cloud_sync_utils import clear_sync_progress
        conn.is_syncing = False
        clear_sync_progress(conn)

    return stats
