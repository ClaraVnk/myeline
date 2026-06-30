"""Infomaniak kDrive connector via WebDAV.

Authentication: Infomaniak email + application password (Basic Auth over HTTPS).
Create an application password in Infomaniak Manager → Security → Application passwords.

WebDAV endpoint: https://{kdrive_id}.connect.kdrive.infomaniak.com
The kDrive ID is visible in the URL on https://ksuite.infomaniak.com/kdrive/app/drive/{ID}/

Flow:
  1. kdrive_connect_form → user enters email, app password, and kDrive ID
  2. store_credentials()  → encrypts all three; kDrive ID goes in access_token_enc (JSON)
  3. list_folders()       → PROPFIND to let user pick a folder
  4. sync_connection()    → recursive PROPFIND + GET for indexable files
"""

import json
import re as _re
from datetime import datetime, timezone

import requests

from app.services.cloud_sync_utils import is_indexable, mime_from_ext
from app.utils.logger import get_logger


logger = get_logger("kdrive_service", subfolder="services")


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def store_credentials(conn, email: str, app_password: str, kdrive_id: str) -> None:
    from app.utils.crypto import encrypt_token
    conn.access_token_enc = encrypt_token(json.dumps({"email": email, "kdrive_id": kdrive_id}))
    conn.refresh_token_enc = encrypt_token(app_password)
    conn.token_expiry = None
    conn.sync_cursor = None


def _get_credentials(conn) -> tuple[str, str, str]:
    """Return (email, app_password, kdrive_id)."""
    from app.utils.crypto import decrypt_token
    data = json.loads(decrypt_token(conn.access_token_enc))
    email = data["email"]
    kdrive_id = data["kdrive_id"]
    password = decrypt_token(conn.refresh_token_enc)
    return email, password, kdrive_id


# ---------------------------------------------------------------------------
# WebDAV helpers
# ---------------------------------------------------------------------------

# kdrive_id doit être strictement alphanumérique. Sans validation, un
# user pouvait soumettre "evil.com/x#" qui aboutirait à
# https://evil.com/x#.connect.kdrive.infomaniak.com — le fragment `#`
# tronque le hostname côté browser/lib HTTP → l'URL réelle devient
# https://evil.com/x → le serveur attaquant collecte l'email + password
# en clair (Basic Auth). Pareil avec "/" : path traversal.
_KDRIVE_ID_REGEX = _re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_kdrive_id(kdrive_id: str) -> None:
    if not _KDRIVE_ID_REGEX.match(kdrive_id or ""):
        raise ValueError(
            f"Invalid kdrive_id {kdrive_id!r} — only [a-zA-Z0-9_-] allowed"
        )


def _base_url(kdrive_id: str) -> str:
    _validate_kdrive_id(kdrive_id)
    return f"https://{kdrive_id}.connect.kdrive.infomaniak.com"


def _dav_url(kdrive_id: str, path: str = "") -> str:
    base = _base_url(kdrive_id)
    if not path or path == "/":
        return base + "/"
    return base + "/" + path.lstrip("/")


def _propfind(auth: tuple, kdrive_id: str, path: str, depth: int = 1) -> list[dict]:
    """Issue a WebDAV PROPFIND and return a list of resource dicts."""
    # Audit H9 / M4: defusedxml hardens against billion-laughs entity
    # expansion attacks that stdlib ElementTree still allows.
    from defusedxml.ElementTree import fromstring as et_fromstring

    url = _dav_url(kdrive_id, path)
    headers = {
        "Depth": str(depth),
        "Content-Type": "application/xml; charset=utf-8",
    }
    body = (
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
    resp = requests.request(
        "PROPFIND", url, auth=auth, headers=headers, data=body, timeout=30
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


def _download(auth: tuple, kdrive_id: str, href: str) -> bytes:
    url = _base_url(kdrive_id) + href if href.startswith("/") else _dav_url(kdrive_id, href)
    resp = requests.get(url, auth=auth, timeout=120)
    resp.raise_for_status()
    return resp.content


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_CRED_OK = "ok"
_CRED_BAD_AUTH = "bad_auth"
_CRED_NO_ACCESS = "no_access"
_CRED_NOT_FOUND = "not_found"
_CRED_ERROR = "error"


def check_credentials(email: str, app_password: str, kdrive_id: str) -> str:
    """Return one of _CRED_* constants describing the authentication result.

    The {id}.connect.kdrive.infomaniak.com endpoint returns 404 when the
    drive ID doesn't exist (before even checking credentials), 401 for bad
    credentials, 403 if the password has no kDrive scope, and 207 on success.
    """
    try:
        url = _dav_url(kdrive_id, "/")
        headers = {
            "Depth": "0",
            "Content-Type": "application/xml; charset=utf-8",
        }
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<propfind xmlns="DAV:"><prop><resourcetype/></prop></propfind>'
        )
        resp = requests.request(
            "PROPFIND", url, auth=(email, app_password),
            headers=headers, data=body, timeout=15,
        )
        logger.debug(
            "kDrive check_credentials: status=%s for %s (drive %s)",
            resp.status_code, email, kdrive_id,
        )
        if resp.status_code == 404:
            return _CRED_NOT_FOUND
        if resp.status_code == 401:
            return _CRED_BAD_AUTH
        if resp.status_code == 403:
            return _CRED_NO_ACCESS
        return _CRED_OK
    except Exception as e:
        logger.warning("kDrive check_credentials exception for %s: %s", email, e)
        return _CRED_ERROR


def list_folders(conn) -> list[dict]:
    """Return top-level directories so the user can pick a sync root."""
    email, password, kdrive_id = _get_credentials(conn)
    auth = (email, password)
    root_path = conn.folder_id or "/"
    try:
        resources = _propfind(auth, kdrive_id, root_path, depth=1)
    except Exception as e:
        logger.error("kDrive list_folders failed: %s", e)
        return []

    folders = []
    for r in resources:
        if r["is_dir"] and r["href"].rstrip("/") != root_path.rstrip("/"):
            folders.append({"id": r["href"], "name": r["name"]})
    return folders


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
        logger.info("kDrive sync %s done: %s", connection_id, stats)
    except Exception:
        logger.exception("kDrive sync %s failed", connection_id)
        stats["errors"] += 1
    finally:
        from app.services.cloud_sync_utils import clear_sync_progress
        conn.is_syncing = False
        clear_sync_progress(conn)

    return stats


def _list_files_recursive(auth: tuple, kdrive_id: str, root_path: str) -> list[dict]:
    """BFS traversal of the WebDAV tree, returning indexable file entries."""
    results = []
    queue = [root_path.rstrip("/") or "/"]
    visited: set[str] = set()

    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        try:
            resources = _propfind(auth, kdrive_id, current, depth=1)
        except Exception as e:
            logger.warning("kDrive PROPFIND failed for %s: %s", current, e)
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

    logger.info("kDrive BFS done: root=%s → %d indexable files", root_path, len(results))
    return results


def _full_sync(conn, stats: dict) -> None:
    from app.ext import db

    email, password, kdrive_id = _get_credentials(conn)
    auth = (email, password)
    root_path = conn.folder_id or "/"
    logger.info("kDrive full_sync connection_id=%s drive=%s root=%s", conn.id, kdrive_id, root_path)

    conn.sync_cursor = datetime.now(timezone.utc).isoformat()
    files = _list_files_recursive(auth, kdrive_id, root_path)

    from app.services.cloud_sync_utils import parallel_download_then_index, start_sync_progress
    start_sync_progress(conn, len(files))
    batch = []
    for item in files:
        descriptor = _build_index_descriptor(conn, auth, kdrive_id, item, stats)
        if descriptor is not None:
            batch.append(descriptor)
    if batch:
        parallel_download_then_index(batch)

    db.session.commit()


def _delta_sync(conn, stats: dict) -> None:
    from app.ext import db
    from app.models.cloud_connection import CloudDocument
    from app.services.cloud_sync_utils import parallel_download_then_index, start_sync_progress

    email, password, kdrive_id = _get_credentials(conn)
    auth = (email, password)
    root_path = conn.folder_id or "/"

    try:
        last_sync = datetime.fromisoformat(conn.sync_cursor).replace(tzinfo=None)
    except (ValueError, TypeError):
        last_sync = None

    files = _list_files_recursive(auth, kdrive_id, root_path)
    # Bug H5 (audit round 2) — l'ancien code avançait sync_cursor AVANT
    # la boucle de traitement. Si une erreur interrompait un download au
    # milieu (`stats["errors"]` incrémenté dans cloud_sync_utils), les
    # items skip n'étaient PAS re-tentés au tick suivant : le cursor avait
    # avancé sur un timestamp qui les couvrait. Régression du même
    # pattern fixé sur Dropbox/OneDrive (M22 round 1). On capture le
    # timestamp candidat ici, on l'applique APRÈS si pas d'erreurs.
    new_cursor_candidate = datetime.now(timezone.utc).isoformat()
    errors_before = stats["errors"]
    start_sync_progress(conn, len(files))

    batch = []
    for item in files:
        remote_id = item["href"]
        existing = CloudDocument.query.filter_by(
            connection_id=conn.id, remote_id=remote_id
        ).first()

        if last_sync and existing and item.get("last_modified"):
            if item["last_modified"] <= last_sync and existing.chunk_count > 0:
                stats["skipped"] += 1
                continue

        descriptor = _build_index_descriptor(conn, auth, kdrive_id, item, stats, existing=existing)
        if descriptor is not None:
            batch.append(descriptor)

    if batch:
        parallel_download_then_index(batch)

    if stats["errors"] == errors_before:
        conn.sync_cursor = new_cursor_candidate
    else:
        logger.warning(
            "kdrive delta sync conn=%s eut %d erreurs — sync_cursor non avancé",
            conn.id, stats["errors"] - errors_before,
        )

    db.session.commit()


def _build_index_descriptor(conn, auth: tuple, kdrive_id: str, item: dict, stats: dict, existing=None):
    """Build kwargs for ``parallel_download_then_index`` or None to skip."""
    from app.services.cloud_sync_utils import _INDEXABLE

    remote_id = item["href"]
    name = item["name"]
    mime = item.get("_mime") or item.get("content_type") or mime_from_ext(name) or ""

    if not is_indexable(mime):
        return None

    ext = _INDEXABLE.get(mime, "")
    size = item.get("size", 0)
    modified_at = item.get("last_modified")

    def download() -> bytes:
        return _download(auth, kdrive_id, remote_id)

    # kDrive's web UI is at https://kdrive.infomaniak.com/app/drive/<id>/files
    # The WebDAV href encodes the path; a reasonable best-effort link points
    # to the parent folder in the web UI.
    from urllib.parse import quote
    web_url = (
        f"https://kdrive.infomaniak.com/app/office/{kdrive_id}/{quote(remote_id.lstrip('/'))}"
        if kdrive_id else None
    )

    return {
        "conn": conn,
        "remote_id": remote_id,
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
