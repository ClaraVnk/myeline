"""Amazon S3 and S3-compatible storage connector.

Authentication: AWS access key + secret key (static credentials).
Works with any S3-compatible endpoint: Amazon S3, Scaleway Object Storage,
OVH Object Storage, Infomaniak kS3, Cloudflare R2, MinIO, etc.

Credential storage in CloudConnection:
  access_token_enc : Fernet-encrypted JSON {"access_key": "...", "secret_key": "..."}
  folder_id        : "<endpoint_url>|<bucket_name>"  (pipe-separated)
  folder_name      : S3 key prefix to restrict indexing (may be empty)
  sync_cursor      : JSON-encoded dict {"etags": {"key": "etag", ...}, "last_sync": "ISO"}
"""

import json
import re as _re
from datetime import datetime, timezone
from typing import Optional

from app.services.cloud_sync_utils import _INDEXABLE, is_indexable, mime_from_ext, process_and_index
from app.utils.logger import get_logger
from app.utils.safe_http import SSRFBlockedError as _SSRFBlocked
from app.utils.safe_http import validate_url


logger = get_logger("s3_service", subfolder="services")

_MAX_KEYS_PER_PAGE = 1000


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def store_credentials(conn, access_key: str, secret_key: str, endpoint_url: str, bucket_name: str) -> None:
    """Encrypt and persist S3 credentials in a CloudConnection (caller must commit)."""
    from app.utils.crypto import encrypt_token

    payload = json.dumps({"access_key": access_key, "secret_key": secret_key})
    conn.access_token_enc = encrypt_token(payload)
    conn.refresh_token_enc = None
    conn.token_expiry = None
    conn.folder_id = f"{endpoint_url}|{bucket_name}"
    conn.sync_cursor = None


def _get_credentials(conn) -> tuple:
    """Return (access_key, secret_key, endpoint_url, bucket_name)."""
    from app.utils.crypto import decrypt_token

    raw = decrypt_token(conn.access_token_enc)
    creds = json.loads(raw)
    access_key = creds["access_key"]
    secret_key = creds["secret_key"]

    folder_id = conn.folder_id or "|"
    parts = folder_id.split("|", 1)
    endpoint_url = parts[0] if parts[0] else None
    bucket_name = parts[1] if len(parts) > 1 else ""

    return access_key, secret_key, endpoint_url, bucket_name


# Whitelist endpoint S3 — protège contre SSRF (bug C3). Sans cette
# validation, un user pouvait pointer endpoint_url vers
# http://169.254.169.254/ ou http://mariadb:3306 et boto3 signait +
# envoyait HeadBucket vers cette URL arbitraire en tant que serveur.
# Patterns autorisés :
#   - https://...amazonaws.com (AWS S3 régions standard + GovCloud + China)
#   - https://...scw.cloud (Scaleway Object Storage)
#   - https://...digitaloceanspaces.com (DigitalOcean Spaces)
#   - https://...wasabisys.com (Wasabi)
#   - https://...backblazeb2.com (Backblaze B2)
#   - https://...cloudflarestorage.com (Cloudflare R2)
#   - https://<own-minio.public.tld>/ — validé via safe_http (rejette IPs
#     internes + DNS rebind) si pas dans la liste explicite ci-dessus.
_S3_ALLOWED_HOSTS_REGEX = _re.compile(
    r"^https://"
    r"(?:"
    r"[a-zA-Z0-9.\-]+\.amazonaws\.com|"
    r"[a-zA-Z0-9.\-]+\.scw\.cloud|"
    r"[a-zA-Z0-9.\-]+\.digitaloceanspaces\.com|"
    r"[a-zA-Z0-9.\-]+\.wasabisys\.com|"
    r"[a-zA-Z0-9.\-]+\.backblazeb2\.com|"
    r"[a-zA-Z0-9.\-]+\.cloudflarestorage\.com"
    r")"
    r"(?::[0-9]+)?/?$"
)


def _validate_endpoint(endpoint_url: str) -> None:
    """Reject SSRF-prone endpoint URLs (bug C3 + M2 audit round 2)."""
    if not endpoint_url:
        return  # default AWS S3
    if _S3_ALLOWED_HOSTS_REGEX.match(endpoint_url):
        return  # known managed S3-compatible provider
    # Bug M2 (audit round 2) — l'ancien fallback `validate_url(endpoint_url)`
    # résolvait l'IP UNE FOIS et la pinnait dans une thread-local map.
    # Mais boto3 fait ses propres connexions hors du context `_pin_dns`,
    # donc ré-résout via DNS et peut atterrir sur 127.0.0.1 si l'attaquant
    # rebind son domaine entre les deux résolutions. La whitelist regex
    # ci-dessus est désormais la SEULE voie d'acceptation ; les vrais
    # MinIO/etc. doivent être ajoutés à `_S3_ALLOWED_HOSTS_REGEX`.
    raise ValueError(
        f"S3 endpoint URL refused: only managed providers (AWS, Scaleway, "
        f"DO Spaces, Wasabi, Backblaze, Cloudflare R2) are accepted. "
        f"Got: {endpoint_url!r}"
    )


def _build_client(conn):
    """Return a boto3 S3 client configured for this connection."""
    import boto3
    from botocore.config import Config

    access_key, secret_key, endpoint_url, _bucket = _get_credentials(conn)

    # A2-L6: TLS verification is on by default in botocore, but make it
    # explicit so a future kwargs refactor can't silently drop it. A
    # self-signed S3 endpoint should fail loudly, not get MITM'd.
    kwargs = {
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
        "verify": True,
        "config": Config(connect_timeout=10, read_timeout=60, retries={"max_attempts": 2}),
    }
    # For non-AWS endpoints, pass the endpoint URL explicitly.
    # For Amazon S3 (https://s3.amazonaws.com or empty), let boto3 use the default.
    if endpoint_url and "amazonaws.com" not in endpoint_url:
        _validate_endpoint(endpoint_url)
        kwargs["endpoint_url"] = endpoint_url

    return boto3.client("s3", **kwargs)


# ---------------------------------------------------------------------------
# Connectivity check (used during form submission)
# ---------------------------------------------------------------------------

def test_connection(access_key: str, secret_key: str, endpoint_url: str, bucket_name: str) -> None:
    """Raise an exception if credentials or bucket are invalid.

    Uses head_bucket which is cheap and requires only s3:ListBucket permission.
    """
    import boto3
    from botocore.config import Config

    kwargs = {
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
        "verify": True,  # A2-L6: see _build_client.
        "config": Config(connect_timeout=8, read_timeout=8, retries={"max_attempts": 1}),
    }
    if endpoint_url and "amazonaws.com" not in endpoint_url:
        _validate_endpoint(endpoint_url)
        kwargs["endpoint_url"] = endpoint_url

    client = boto3.client("s3", **kwargs)
    client.head_bucket(Bucket=bucket_name)


# ---------------------------------------------------------------------------
# Object listing
# ---------------------------------------------------------------------------

def _list_objects(client, bucket: str, prefix: str) -> list:
    """Return all S3 objects under *prefix* that have an indexable extension."""
    results = []
    paginator = client.get_paginator("list_objects_v2")
    page_kwargs = {"Bucket": bucket, "PaginationConfig": {"PageSize": _MAX_KEYS_PER_PAGE}}
    if prefix:
        page_kwargs["Prefix"] = prefix

    for page in paginator.paginate(**page_kwargs):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            # Skip "folder" pseudo-objects (keys ending with /)
            if key.endswith("/"):
                continue
            mime = mime_from_ext(key)
            if not mime or not is_indexable(mime):
                continue
            obj["_mime"] = mime
            results.append(obj)

    return results


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync_connection(connection_id: int) -> dict:
    """Full or delta sync for a CloudConnection with provider='s3'.

    Returns {"indexed": int, "skipped": int, "deleted": int, "errors": int}.
    Compatible with RQ worker (no current_app access at module level).
    """
    from app.ext import db
    from app.models.cloud_connection import CloudConnection

    conn = db.session.get(CloudConnection, connection_id)
    if not conn:
        return {"error": "Connection not found"}

    conn.is_syncing = True
    db.session.commit()

    stats = {"indexed": 0, "skipped": 0, "deleted": 0, "errors": 0}
    try:
        _, _, _, bucket_name = _get_credentials(conn)
        if not bucket_name:
            raise ValueError("Bucket name is missing from connection configuration")

        client = _build_client(conn)
        prefix = (conn.folder_name or "").strip()

        objects = _list_objects(client, bucket_name, prefix)

        from app.services.cloud_sync_utils import bump_sync_progress, start_sync_progress
        start_sync_progress(conn, len(objects))

        # Load existing ETag cursor
        cursor: dict = {}
        if conn.sync_cursor:
            try:
                cursor = json.loads(conn.sync_cursor)
            except (json.JSONDecodeError, TypeError):
                cursor = {}

        known_etags: dict = cursor.get("etags", {})
        new_etags: dict = {}

        for obj in objects:
            key = obj["Key"]
            etag = obj.get("ETag", "").strip('"')
            mime = obj["_mime"]
            ext = _INDEXABLE[mime]
            size = int(obj.get("Size", 0) or 0)
            last_modified = obj.get("LastModified")
            modified_at: Optional[datetime] = None
            if isinstance(last_modified, datetime):
                modified_at = last_modified.replace(tzinfo=None)

            new_etags[key] = etag

            # Delta: skip if ETag unchanged and already indexed
            old_etag = known_etags.get(key)
            if old_etag and old_etag == etag:
                # Check DB to confirm it is actually indexed
                from app.models.cloud_connection import CloudDocument
                existing = CloudDocument.query.filter_by(
                    connection_id=conn.id, remote_id=key
                ).first()
                if existing and existing.chunk_count > 0:
                    stats["skipped"] += 1
                    bump_sync_progress(conn)
                    continue

            # Fetch existing record for upsert
            from app.models.cloud_connection import CloudDocument
            existing = CloudDocument.query.filter_by(
                connection_id=conn.id, remote_id=key
            ).first()

            # Capture variables for closure
            _client = client
            _bucket = bucket_name
            _key = key

            def _download(_c=_client, _b=_bucket, _k=_key) -> bytes:
                response = _c.get_object(Bucket=_b, Key=_k)
                return response["Body"].read()

            # Best-effort web URL — for AWS S3 use the region-aware console
            # path; for S3-compatible providers (Scaleway, OVH, R2, MinIO)
            # build a URL relative to the configured endpoint.
            from urllib.parse import quote
            _, _, ep, _ = _get_credentials(conn)
            if ep and "amazonaws.com" not in ep:
                web_url = f"{ep.rstrip('/')}/{bucket_name}/{quote(key)}"
            else:
                web_url = f"https://s3.console.aws.amazon.com/s3/object/{bucket_name}?prefix={quote(key)}"

            process_and_index(
                conn,
                remote_id=key,
                name=key.split("/")[-1] or key,
                mime=mime,
                size=size,
                modified_at=modified_at,
                download_fn=_download,
                ext=ext,
                stats=stats,
                existing=existing,
                web_url=web_url,
            )

        # Handle deletions: objects in the old cursor that no longer exist
        current_keys = {obj["Key"] for obj in objects}
        removed_keys = set(known_etags.keys()) - current_keys
        for key in removed_keys:
            from app.models.cloud_connection import CloudDocument
            doc = CloudDocument.query.filter_by(connection_id=conn.id, remote_id=key).first()
            if doc:
                from app.services.cloud_sync_utils import remove_cloud_doc
                remove_cloud_doc(doc)
                stats["deleted"] += 1

        # Persist new cursor
        conn.sync_cursor = json.dumps({"etags": new_etags, "last_sync": datetime.now(timezone.utc).isoformat()})
        conn.last_sync_at = datetime.now(timezone.utc)
        db.session.commit()
        logger.info("S3 sync %d done: %s", connection_id, stats)

    except Exception as e:
        logger.exception("S3 sync %d failed: %s", connection_id, e)
        stats["errors"] += 1
    finally:
        from app.services.cloud_sync_utils import clear_sync_progress
        conn.is_syncing = False
        clear_sync_progress(conn)

    return stats
