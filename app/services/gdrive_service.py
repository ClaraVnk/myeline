"""Google Drive sync service — server-side service account.

Setup (one-time, operator):
  1. Create a GCP service account in the Google Cloud Console.
  2. Grant it no roles (it only needs Drive access via file sharing).
  3. Download the JSON key and set GOOGLE_SERVICE_ACCOUNT_CREDENTIALS
     to its full content (single-line JSON, or multi-line in .env).

Setup (per user):
  4. User shares their Drive folder with the service account email.
  5. User pastes the folder URL in Myeline's connect form.
  6. Myeline validates access and starts polling on the next cron tick.

This approach uses no user OAuth tokens and no sensitive Drive scopes,
so no CASA Tier 2 audit is required.
"""

import io
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.utils.logger import get_logger


logger = get_logger("gdrive_service", subfolder="services")

_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

_INDEXABLE: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".docx",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/vnd.oasis.opendocument.text": ".odt",
}

_GDOCS_EXPORT: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
}

_MAX_BYTES = 50 * 1024 * 1024
_DOWNLOAD_CONCURRENCY = 4


# ---------------------------------------------------------------------------
# Service account helpers
# ---------------------------------------------------------------------------

def get_service_account_email() -> Optional[str]:
    """Return the service account email from config, or None if not configured."""
    from flask import current_app
    raw = current_app.config.get("GOOGLE_SERVICE_ACCOUNT_CREDENTIALS", "")
    if not raw:
        return None
    try:
        return json.loads(raw).get("client_email")
    except (json.JSONDecodeError, AttributeError):
        return None


def _build_service():
    """Return an authenticated Drive v3 service using the server-side service account."""
    import google.oauth2.service_account
    from flask import current_app
    from googleapiclient.discovery import build

    raw = current_app.config.get("GOOGLE_SERVICE_ACCOUNT_CREDENTIALS", "")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_CREDENTIALS n'est pas configuré.")

    info = json.loads(raw)
    creds = google.oauth2.service_account.Credentials.from_service_account_info(
        info, scopes=[_DRIVE_SCOPE]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def validate_folder_access(folder_id: str) -> dict:
    """Check the service account can access folder_id. Returns folder metadata.

    Raises RuntimeError with a user-facing message if inaccessible.
    """
    service = _build_service()
    try:
        f = service.files().get(
            fileId=folder_id,
            fields="id,name,mimeType",
        ).execute()
    except Exception as e:
        raise RuntimeError(
            "Impossible d'accéder au dossier. Avez-vous bien partagé le dossier "
            "avec l'adresse de notre compte de service ?"
        ) from e

    if f.get("mimeType") != "application/vnd.google-apps.folder":
        raise RuntimeError("L'identifiant fourni n'est pas un dossier Google Drive.")

    return f


def extract_folder_id(url_or_id: str) -> str:
    """Extract folder ID from a Drive URL, or return the raw value as-is."""
    import re
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", url_or_id)
    if m:
        return m.group(1)
    return url_or_id.strip()


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync_connection(connection_id: int) -> dict:
    """Polling sync for a CloudConnection. Returns stats dict."""
    from app.ext import db
    from app.models.cloud_connection import CloudConnection

    conn = db.session.get(CloudConnection, connection_id)
    if not conn:
        return {"error": "Connection not found"}

    conn.is_syncing = True
    db.session.commit()

    stats = {"indexed": 0, "skipped": 0, "deleted": 0, "errors": 0}
    try:
        service = _build_service()
        _polling_sync(conn, service, stats)
        conn.last_sync_at = datetime.now(timezone.utc)
        db.session.commit()
        logger.info(
            "Sync connection %d done: %d indexed, %d deleted, %d errors",
            connection_id, stats["indexed"], stats["deleted"], stats["errors"],
        )
    except Exception as e:
        logger.exception("Sync connection %d failed: %s", connection_id, e)
        stats["errors"] += 1
    finally:
        from app.services.cloud_sync_utils import clear_sync_progress
        conn.is_syncing = False
        clear_sync_progress(conn)

    return stats


def _polling_sync(conn, service, stats: dict) -> None:
    """List all files in the shared folder, index new/updated, remove deleted.

    On each run: BFS the shared folder, compare with DB records.
    Files in Drive but not in DB → index.
    Files in DB but not in Drive → remove.
    Files in both → skip if modifiedTime unchanged, re-index otherwise.
    """
    from app.models.cloud_connection import CloudDocument
    from app.services.cloud_sync_utils import start_sync_progress

    files = _list_files(service, conn.folder_id)
    remote_ids = {f["id"] for f in files}
    start_sync_progress(conn, len(files))

    stored_docs = CloudDocument.query.filter_by(connection_id=conn.id).all()
    stored_map = {doc.remote_id: doc for doc in stored_docs}

    # Remove files that are no longer in the Drive folder
    for remote_id, doc in list(stored_map.items()):
        if remote_id not in remote_ids:
            _remove_cloud_doc(doc)
            stats["deleted"] += 1

    _process_files_parallel(conn, service, files, stats, existing_map=stored_map)


def _list_files(service, folder_id: Optional[str]) -> list[dict]:
    """Return all indexable files under folder_id (recursive BFS).

    The service account can only access files shared with it — no ownership
    filter is applied. Returns an empty list if folder_id is not set, since
    the service account has no access without an explicit shared folder.
    """
    if not folder_id:
        return []

    results = []
    queue = [folder_id]
    visited_folders: set[str] = set()

    while queue:
        parent = queue.pop(0)
        page_token = None

        while True:
            q = f"'{parent}' in parents and trashed=false"
            resp = (
                service.files()
                .list(
                    q=q,
                    fields="nextPageToken,files(id,name,mimeType,size,modifiedTime,webViewLink)",
                    pageSize=200,
                    pageToken=page_token,
                )
                .execute()
            )

            for f in resp.get("files", []):
                if f["mimeType"] == "application/vnd.google-apps.folder":
                    fid = f["id"]
                    if fid not in visited_folders:
                        visited_folders.add(fid)
                        queue.append(fid)
                elif _is_indexable(f["mimeType"]):
                    results.append(f)

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    return results


def _process_files_parallel(conn, service, files: list, stats: dict, existing_map: Optional[dict] = None) -> None:
    """Download files in parallel, index sequentially as each completes.

    googleapiclient builds its HTTP layer on httplib2, whose Http instance
    is not thread-safe — sharing one across worker threads triggers SSL
    record-layer failures under load. Build a fresh service per thread.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from flask import current_app

    if not files:
        return

    app = current_app._get_current_object()

    def _dl(f):
        with app.app_context():
            try:
                thread_service = _build_service()
                return (f, _download_file(thread_service, f["id"], f.get("mimeType", "")), None)
            except Exception as exc:
                return (f, None, exc)

    existing_map = existing_map or {}

    from app.services.cloud_sync_utils import bump_sync_progress

    with ThreadPoolExecutor(max_workers=_DOWNLOAD_CONCURRENCY) as pool:
        futures = [pool.submit(_dl, f) for f in files]
        for fut in as_completed(futures):
            f, payload, exc = fut.result()
            if exc is not None:
                logger.error("Download failed for %s: %s", f.get("name"), exc)
                stats["errors"] += 1
                try:
                    bump_sync_progress(conn)
                except Exception:
                    logger.exception("Failed to bump sync progress on download error")
                continue
            _process_file(
                conn, service, f, stats,
                content_and_ext=payload,
                existing=existing_map.get(f.get("id")),
            )


def _is_indexable(mime: str) -> bool:
    return mime in _INDEXABLE or mime in _GDOCS_EXPORT


def _process_file(conn, service, file_meta: dict, stats: dict, existing=None, content_and_ext=None) -> None:
    """Download (if needed), extract, and index a single file."""
    from app.ext import db
    from app.models.cloud_connection import CloudDocument
    from app.services.cloud_sync_utils import bump_sync_progress
    from app.services.rag_service import index_cloud_document, remove_cloud_document_from_index
    from app.utils.time_utils import utcnow

    bump_sync_progress(conn)

    file_id = file_meta.get("id") or file_meta.get("fileId")
    name = file_meta.get("name", "unknown")
    mime = file_meta.get("mimeType", "")
    size = int(file_meta.get("size", 0) or 0)
    modified_str = file_meta.get("modifiedTime")

    if size > _MAX_BYTES:
        logger.info("Skipping %s: too large (%d bytes)", name, size)
        stats["skipped"] += 1
        return

    modified_at = None
    if modified_str:
        try:
            modified_at = datetime.fromisoformat(modified_str.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass

    if existing and existing.remote_modified_at == modified_at and existing.chunk_count > 0:
        stats["skipped"] += 1
        return

    if content_and_ext is not None:
        content, ext = content_and_ext
    else:
        try:
            content, ext = _download_file(service, file_id, mime)
        except Exception as e:
            logger.error("Download failed for %s (%s): %s", name, file_id, e)
            stats["errors"] += 1
            return

    if existing:
        doc = existing
        if doc.chunk_count > 0:
            remove_cloud_document_from_index(conn.user_id, doc.id, doc.chunk_count)
    else:
        doc = CloudDocument(
            connection_id=conn.id,
            user_id=conn.user_id,
            remote_id=file_id,
        )
        db.session.add(doc)

    doc.name = name
    doc.mime_type = mime
    doc.file_size = size
    doc.remote_modified_at = modified_at
    doc.web_url = file_meta.get("webViewLink") or None
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


def _download_file(service, file_id: str, mime: str) -> tuple[bytes, str]:
    """Return (file_bytes, extension)."""
    from googleapiclient.http import MediaIoBaseDownload

    if mime in _GDOCS_EXPORT:
        export_mime, ext = _GDOCS_EXPORT[mime]
        request = service.files().export_media(fileId=file_id, mimeType=export_mime)
    else:
        ext = _INDEXABLE.get(mime, ".bin")
        request = service.files().get_media(fileId=file_id)

    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    return buf.getvalue(), ext


def _remove_cloud_doc(doc) -> None:
    from app.ext import db
    from app.services.rag_service import remove_cloud_document_from_index

    if doc.chunk_count > 0:
        remove_cloud_document_from_index(doc.user_id, doc.id, doc.chunk_count)
    db.session.delete(doc)
    db.session.commit()
