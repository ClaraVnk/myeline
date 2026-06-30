"""Cron: archive_audit_log
Schedule: weekly at 03:30 on Sunday
Purpose: write every audit row older than AUDIT_RETENTION_DAYS to an
         S3-compatible object store (immutable WORM bucket on
         compliance-grade infra), then delete those rows from the
         hot DB so the table doesn't grow unbounded.

The hot DB keeps a recent window for fast querying from /admin/audit;
the off-host archive is the long-term, tamper-evident record an
auditor will ask for during a SOC2 / ISO-27001 review.

No-op when AUDIT_S3_BUCKET is not set — the audit log just stays in
the hot DB forever (acceptable for small deployments).
"""
import json
import os
from datetime import datetime, timedelta, timezone

from app.utils.logger import get_logger


CRON_SCHEDULE = "30 3 * * 0"
CRON_DESCRIPTION = "Weekly archive of audit log to S3 + DB prune"

logger = get_logger("archive_audit_log", subfolder="cron")

_RETENTION_DAYS = int(os.getenv("AUDIT_RETENTION_DAYS", "180"))
_BATCH = 5000


def _s3_client():
    """Build a boto3 S3 client from env. Returns (client, bucket) or
    (None, None) when not configured."""
    bucket = os.getenv("AUDIT_S3_BUCKET", "")
    if not bucket:
        return None, None
    try:
        import boto3
        endpoint = os.getenv("AUDIT_S3_ENDPOINT") or None
        # Bug M6 (audit round 2) — l'endpoint était passé brut à boto3
        # sans validation. Un opérateur (ou attaquant maîtrisant l'env)
        # pouvait pointer vers un endpoint interne → audit contenant
        # emails/IPs/payload livré à un service tiers. Réutiliser la
        # whitelist hosts du module s3_service (cohérent avec C3/M2).
        if endpoint:
            from app.services.s3_service import _validate_endpoint
            _validate_endpoint(endpoint)
        region = os.getenv("AUDIT_S3_REGION") or "eu-west-3"
        access_key = os.getenv("AUDIT_S3_ACCESS_KEY")
        secret_key = os.getenv("AUDIT_S3_SECRET_KEY")
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        return client, bucket
    except Exception as exc:
        logger.error("S3 client init failed: %s", exc)
        return None, None


def _serialise(row) -> dict:
    return {
        "id": row.id,
        "actor_user_id": row.actor_user_id,
        "actor_email": row.actor_email,
        "action": row.action,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "payload_json": row.payload_json,
        "ip_address": row.ip_address,
        "user_agent": row.user_agent,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def run() -> dict:
    from app.ext import db
    from app.models.audit_log import AuditLog

    client, bucket = _s3_client()
    if not client:
        logger.info("AUDIT_S3_BUCKET not set — archive cron is a no-op")
        return {"archived": 0, "skipped": True}

    cutoff = datetime.now(timezone.utc) - timedelta(days=_RETENTION_DAYS)
    total_archived = 0

    while True:
        rows = (
            db.session.query(AuditLog)
            .filter(AuditLog.created_at < cutoff)
            .order_by(AuditLog.id.asc())
            .limit(_BATCH)
            .all()
        )
        if not rows:
            break

        # One JSONL object per batch — keys partitioned by date so a
        # subpoena scoped to a date range stays cheap to retrieve.
        first_iso = rows[0].created_at.date().isoformat()
        last_iso = rows[-1].created_at.date().isoformat()
        key = f"audit/{first_iso}_to_{last_iso}_id{rows[0].id}-{rows[-1].id}.jsonl"
        body = "\n".join(
            json.dumps(_serialise(r), ensure_ascii=False, default=str) for r in rows
        ).encode("utf-8")

        # Bug H18 — sur un bucket Object Lock / WORM, si put_object a
        # réussi au run précédent mais que le DB delete a échoué, la
        # même clé est ré-uploadée au run suivant → refusé par WORM,
        # boucle infinie sur le même batch. Fix : head_object d'abord ;
        # si la clé existe déjà avec la bonne taille, on saute le put
        # et on enchaîne directement sur le delete DB pour rattraper
        # l'état incohérent.
        already_uploaded = False
        try:
            head = client.head_object(Bucket=bucket, Key=key)
            if head.get("ContentLength") == len(body):
                already_uploaded = True
                logger.info("S3 key %s already exists with matching size — skipping put", key)
            else:
                # Même clé, taille différente : on ne peut pas overwrite
                # sur WORM. On log et on arrête plutôt que de boucler.
                logger.error(
                    "S3 key %s exists with different size (have=%s, want=%s) — aborting",
                    key, head.get("ContentLength"), len(body),
                )
                return {"archived": total_archived, "error": "size_mismatch"}
        except client.exceptions.ClientError as exc:
            err_code = exc.response.get("Error", {}).get("Code", "")
            if err_code not in ("404", "NoSuchKey", "NotFound"):
                logger.error("S3 head_object failed for %s: %s", key, exc)
                return {"archived": total_archived, "error": str(exc)}
        except Exception as exc:
            logger.error("S3 head_object failed for %s: %s", key, exc)
            return {"archived": total_archived, "error": str(exc)}

        if not already_uploaded:
            try:
                client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=body,
                    ContentType="application/jsonl",
                    # Object Lock / WORM is configured at the bucket level
                    # in compliance-grade buckets; we don't try to set it
                    # per-object here.
                )
            except Exception as exc:
                logger.error("S3 upload failed for %s: %s", key, exc)
                return {"archived": total_archived, "error": str(exc)}

        ids = [r.id for r in rows]
        try:
            db.session.query(AuditLog).filter(AuditLog.id.in_(ids)).delete(
                synchronize_session=False,
            )
            db.session.commit()
        except Exception as exc:
            logger.error("DB prune failed for batch %s: %s", key, exc)
            db.session.rollback()
            return {"archived": total_archived, "error": str(exc)}

        total_archived += len(rows)
        logger.info("Archived %d rows to s3://%s/%s", len(rows), bucket, key)

    logger.info("Audit archive complete — %d rows archived", total_archived)
    return {"archived": total_archived, "skipped": False}
