"""GDPR helpers — Article 17 (right to erasure) compliance.

Bug CC5 (audit round 2) — l'audit_log conserve `actor_email` en clair
pour TOUTES les actions historiques d'un user, et la colonne est
`nullable=False`. Quand un user est supprimé (manuellement, via cron
RGPD, ou via /user/delete_account), l'email reste donc indéfiniment
dans `audit_logs.actor_email`, violant le droit à l'effacement.

Ce module fournit un helper qui :
  1. Écrit une dernière ligne d'audit `user.gdpr_purge` AVANT le
     delete (traçabilité de la suppression elle-même).
  2. Scrub les `audit_logs.actor_email` du user vers un placeholder
     déterministe (`deleted-<hash>`) — préserve la cohérence pour
     forensics multi-event ("plusieurs actions par le même
     deleted-XYZ") sans conserver l'identifiant personnel.

Appelé par :
  - app/cron/cleanup_rgpd.py
  - app/cron/data_retention_rgpd.py
  - app/routes/user_tools.py (delete_account)
  - app/routes/admin/manage_users.py (delete_user)
"""
import hashlib

from app.ext import db
from app.models.audit_log import AuditLog
from app.utils.audit import log_audit
from app.utils.logger import get_logger


logger = get_logger("gdpr", subfolder="utils")


def _deterministic_placeholder(email: str) -> str:
    """Return a stable, non-reversible placeholder for a deleted email.

    `deleted-<16 hex>` fits the 255-char column comfortably, and SHA-256
    over a 16-hex prefix gives ~64 bits of collision resistance — enough
    for the scale of audit logs (millions of events × tens of thousands
    of users) without leaking the original email.
    """
    digest = hashlib.sha256(email.lower().encode("utf-8")).hexdigest()[:16]
    return f"deleted-{digest}"


def scrub_user_audit_trail(user, reason: str = "retention") -> None:
    """Anonymise an user's PII in `audit_logs` before their row is deleted.

    Call this BEFORE `db.session.delete(user)`. The caller is responsible
    for committing — this function only stages the updates.

    `reason` ∈ {"retention", "unconfirmed_cleanup", "self_delete",
    "admin_delete"} — recorded in the final audit row payload.
    """
    if not user or not user.email:
        return

    placeholder = _deterministic_placeholder(user.email)

    # Last trail entry BEFORE we strip the email. `log_audit` itself
    # snapshots the actor email — but since we want this very row to
    # already wear the placeholder (the user is being purged), we craft
    # it manually with the placeholder email.
    try:
        gdpr_row = AuditLog(
            actor_user_id=None,  # FK will go to NULL anyway via SET NULL
            actor_email=placeholder,
            action="user.gdpr_purge",
            target_type="user",
            target_id=str(user.id),
            payload_json=f'{{"reason": "{reason}", "user_id": {user.id}}}',
        )
        db.session.add(gdpr_row)
    except Exception:
        logger.exception("Failed to write gdpr_purge audit row for user_id=%s", user.id)

    # Scrub past audit rows: actor_email cleared on every row that
    # referenced this user. actor_user_id is cascaded to NULL by FK ON
    # DELETE SET NULL — covered by the existing schema.
    #
    # Audit round 4 — A4-C1 / A1-C1. The previous scrub only cleared
    # actor_email and left ip_address + user_agent intact. After J+59
    # an attacker with backup access could still reconstruct a deleted
    # user's session footprint (every IPv4/v6 they touched the app from,
    # every browser fingerprint they used). RGPD Art. 17 expects every
    # piece of personal data — not just the email — to be removed on the
    # right-to-erasure request. We null both columns at the same time
    # so the deterministic placeholder still allows correlating "the
    # same deleted-XYZ did N actions" without exposing PII.
    try:
        updated = (
            db.session.query(AuditLog)
            .filter(AuditLog.actor_user_id == user.id)
            .update(
                {
                    "actor_email": placeholder,
                    "actor_user_id": None,
                    "ip_address": None,
                    "user_agent": None,
                },
                synchronize_session=False,
            )
        )
        logger.info(
            "GDPR scrub: anonymised %d audit_log row(s) for user_id=%s (reason=%s)",
            updated, user.id, reason,
        )
    except Exception:
        logger.exception("Failed to scrub audit trail for user_id=%s", user.id)


def log_and_delete_user(user, reason: str) -> bool:
    """Atomic-ish helper: scrub audit + delete + commit. Returns True on success."""
    user_id = user.id
    try:
        scrub_user_audit_trail(user, reason=reason)
        db.session.delete(user)
        db.session.commit()
        logger.info("GDPR purge complete user_id=%s reason=%s", user_id, reason)
        return True
    except Exception:
        db.session.rollback()
        logger.exception("GDPR purge failed for user_id=%s", user_id)
        return False


# Kept for forward-compat; not currently used.
__all__ = ["scrub_user_audit_trail", "log_and_delete_user"]
