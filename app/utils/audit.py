"""log_audit() — central helper for the AuditLog table.

Callers describe the action symbolically (e.g. "org.delete",
"broadcast.send", "ticket.status_changed"); the helper resolves the
acting user from Flask-Login + request, snapshots the email, and writes
an append-only row.

The helper never raises — audit failures must not break the action they
audit. Error-on-write is logged at WARNING so it stays visible without
masking the user's response.
"""
import json
from typing import Any, Optional

from app.utils.logger import get_logger


logger = get_logger("audit", subfolder="utils")


def log_audit(
    action: str,
    *,
    target_type: Optional[str] = None,
    target_id: Optional[Any] = None,
    payload: Optional[dict] = None,
    actor=None,
) -> None:
    """Persist an audit-log row for a sensitive action.

    Pass ``actor`` explicitly when the action runs outside a request
    context (e.g. a CLI command); otherwise the current Flask-Login
    user is used.

    Resource pointers (target_type / target_id) are free-form strings
    — keep them stable across versions ("organization" not "org_v2").
    The payload dict is JSON-serialised; non-JSON-safe values are
    coerced via ``str(...)`` rather than failing the audit.
    """
    try:
        from app.ext import db
        from app.models.audit_log import AuditLog
        from app.utils.time_utils import utcnow

        # Resolve actor — explicit > current_user > anonymous.
        if actor is None:
            try:
                from flask_login import current_user
                if current_user.is_authenticated:
                    actor = current_user._get_current_object()
            except Exception:
                actor = None

        actor_id = getattr(actor, "id", None)
        actor_email = getattr(actor, "email", None) or "system"

        # Request-scoped context (IP, UA) — only available inside a
        # request. None for CLI / cron paths.
        ip = ua = None
        try:
            from flask import request
            ip = request.remote_addr
            ua = (request.user_agent.string or "")[:255]
        except Exception:
            pass

        try:
            payload_str = json.dumps(payload, ensure_ascii=False, default=str) if payload else None
        except Exception as exc:
            logger.warning("audit payload not JSON-serialisable for %s: %s", action, exc)
            payload_str = None

        entry = AuditLog(
            actor_user_id=actor_id,
            actor_email=actor_email[:255],
            action=action[:80],
            target_type=(target_type or None) and target_type[:40],
            target_id=(str(target_id)[:80] if target_id is not None else None),
            payload_json=payload_str,
            ip_address=ip,
            user_agent=ua,
            created_at=utcnow(),
        )
        db.session.add(entry)
        # Flush only — the surrounding transaction will commit or rollback
        # the action together with its audit row. If the action fails and
        # rolls back, the audit row goes with it, which is what we want
        # (don't audit something that didn't happen).
        db.session.flush()
        # Mirror to Prometheus for ops dashboards (uses the same action
        # symbol as the audit row so labels line up across systems).
        try:
            from app.utils.metrics import audit_events_total
            audit_events_total.labels(action=action[:80]).inc()
        except Exception:
            pass
    except Exception as exc:
        logger.warning("audit log write failed for %s: %s", action, exc)
