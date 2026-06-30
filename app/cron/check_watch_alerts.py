"""
Cron job: check_watch_alerts

For each active WatchAlert, run a RAG search restricted to documents indexed
since last_notified_at (or since alert creation if never notified).
If results are found, email the user and update last_notified_at.

Schedule: daily (run twice a day; frequency filtering is done per-alert).
"""
import os
import traceback
from datetime import timedelta

from flask.cli import AppGroup

from app.ext import db
from app.models.watch_alert import WatchAlert
from app.utils.logger import get_logger
from app.utils.time_utils import utcnow


logger = get_logger("check_watch_alerts", subfolder="cron")

CRON_SCHEDULE = "0 8,20 * * *"
CRON_DESCRIPTION = "Keyword watch alert email digests (twice daily, user-time-of-day)"

cron_cli = AppGroup("cron")

_FREQUENCY_DELTA = {
    "daily": timedelta(hours=20),   # slight buffer to avoid drift
    "weekly": timedelta(days=6),
}


@cron_cli.command("check-watch-alerts")
def check_watch_alerts():
    logger.info("Lancement de check_watch_alerts")
    try:
        from sqlalchemy.orm import joinedload
        now = utcnow()
        # Eager-load `user` so _send_alert_email below doesn't fire one
        # SELECT per alert (perf audit H2). joinedload is OK here — the
        # alert→user cardinality is N-to-1.
        alerts = (
            WatchAlert.query
            .options(joinedload(WatchAlert.user))
            .filter_by(is_active=True)
            .all()
        )
        logger.info("%d alerte(s) active(s) à vérifier", len(alerts))

        notified = 0
        for alert in alerts:
            try:
                delta = _FREQUENCY_DELTA.get(alert.frequency, timedelta(hours=20))
                last = alert.last_notified_at or alert.created_at
                if (now - last) < delta:
                    continue

                results = _search_recent(alert, since=last)
                if not results:
                    # Update timestamp so we don't recheck the same window next time
                    alert.last_notified_at = now
                    # Bug M5 (audit round 2) — commit par alert : sur
                    # un crash mid-loop, l'ancien commit global perdait
                    # tous les `last_notified_at` posés → re-envoi des
                    # mêmes alertes au prochain tick.
                    db.session.commit()
                    continue

                _send_alert_email(alert, results, now)
                alert.last_notified_at = now
                db.session.commit()
                notified += 1
                logger.info("Alerte notifiée alert_id=%s user_id=%s keyword=%r", alert.id, alert.user_id, alert.keyword)

            except Exception:
                db.session.rollback()
                logger.error("Erreur alerte alert_id=%s : %s", alert.id, traceback.format_exc())

        logger.info("check_watch_alerts terminé — %d notification(s) envoyée(s)", notified)

    except Exception:
        # Bug L12 — ancien comportement : log.error top-level sans alerte
        # ops, les watch alerts pouvaient être down des semaines sans
        # qu'on le voie. Désormais on log.critical (remontée plus
        # visible dans les dashboards) et on tente une alerte mail si
        # DEV_EMAIL est configuré.
        err = traceback.format_exc()
        logger.critical("Erreur fatale check_watch_alerts : %s", err)
        dev_email = os.getenv("DEV_EMAIL")
        if dev_email:
            try:
                from app.utils.mailer import send_email_via_brevo_api
                send_email_via_brevo_api(
                    to=dev_email,
                    subject="❌ check_watch_alerts a planté",
                    html_content=f"<pre>{err}</pre>",
                )
            except Exception:
                logger.exception("Failed to dispatch check_watch_alerts failure alert")


def _search_recent(alert: WatchAlert, since) -> list:
    """Return matching sources for the alert keyword restricted to recent content."""
    try:
        from app.services.rag_service import search

        # Watch alerts only consume `sources` — synthesis is discarded.
        # Skipping the LLM synthesis call cuts 5-30 s per alert and the
        # entire Mistral synthesis bill on a watch-alerts cron tick
        # (perf audit H2).
        result = search(
            query=alert.keyword,
            scope=_resolve_scope(alert),
            user_id=alert.user_id,
            top_k=6,
            synthesis=False,
        )
        sources = result.get("sources") or []
        # Filter to sources that have a date newer than `since`, when available.
        # Sources without a date are always included (conservative).
        filtered = []
        for src in sources:
            src_date = src.get("date") or src.get("published_at") or src.get("indexed_at")
            if src_date:
                try:
                    from datetime import datetime
                    if isinstance(src_date, str):
                        # Try ISO parse; fall back to including the source
                        parsed = datetime.fromisoformat(src_date.replace("Z", "+00:00")).replace(tzinfo=None)
                        if parsed <= since:
                            continue
                except Exception:
                    pass
            filtered.append(src)

        return filtered[:5]
    except Exception as e:
        logger.warning("Erreur recherche alerte alert_id=%s : %s", alert.id, e)
        return []


def _resolve_scope(alert: WatchAlert) -> str:
    """Map alert scope to a rag_service scope string."""
    mapping = {
        "shared": "shared",
        "personal": "personal",
        "scrapers": "scrapers",
        "all": "all",
    }
    return mapping.get(alert.scope, "all")


def _send_alert_email(alert: WatchAlert, sources: list, now) -> None:
    from flask import render_template

    from app.utils.mailer import send_email_via_brevo_api

    user = alert.user
    if not user or not user.email:
        return

    html = render_template(
        "emails/watch_alert.html",
        user=user,
        alert=alert,
        sources=sources,
        checked_at=now,
    )
    send_email_via_brevo_api(
        to=user.email,
        subject=f'🔔 Alerte Myeline : {alert.keyword}',
        html_content=html,
    )
