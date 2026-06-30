import os
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from dotenv import load_dotenv
from flask import render_template
from flask.cli import AppGroup

from app.models.user import User
from app.utils.logger import get_logger
from app.utils.mailer import send_email_via_brevo_api
from app.utils.time_utils import utcnow


load_dotenv()

logger = get_logger("remind_unconfirmed_users", subfolder="cron")

CRON_SCHEDULE = "0 10 * * *"
CRON_DESCRIPTION = "Daily reminder email to accounts unconfirmed for 24h+"

cron_cli = AppGroup("cron")


def _claim_reminder_slot(user_id: int, kind: str, today_iso: str) -> bool:
    """Try to claim a per-day idempotency slot for this reminder.

    Returns True when the slot is freshly claimed (the caller should send
    the email), False when another run has already taken it today.

    Failing open on Redis errors is intentional — losing one reminder is
    worse than skipping the cron entirely on infra hiccups.
    """
    try:
        from redis import Redis

        url = os.getenv("REDIS_URL", "redis://redis:6379/0")
        redis_conn = Redis.from_url(url, socket_connect_timeout=1)
        key = f"reminder:{kind}:{user_id}:{today_iso}"
        # 25-hour TTL gives us a safety margin even if the cron drifts.
        return bool(redis_conn.set(key, "1", nx=True, ex=25 * 3600))
    except Exception as exc:
        logger.warning("Redis idempotency check failed (failing open): %s", exc)
        return True


@cron_cli.command("remind-unconfirmed-users")
def remind_unconfirmed_users():
    logger.info("🚀 Lancement de la tâche remind_unconfirmed_users")
    try:
        now = utcnow()
        today_iso = now.date().isoformat()

        # Pass 1 — relance J+1 : inscrits il y a plus de 24h, moins de 23 jours
        seuil_rappel_min = now - timedelta(days=23)
        seuil_rappel_max = now - timedelta(days=1)
        users_rappel = User.query.filter(
            User.is_confirmed.is_(False),
            User.created_at >= seuil_rappel_min,
            User.created_at < seuil_rappel_max,
        ).all()

        # Pass 2 — avertissement suppression : inscrits entre J-30 et J-23
        seuil_suppr_min = now - timedelta(days=30)
        seuil_suppr_max = now - timedelta(days=23)
        users_suppr = User.query.filter(
            User.is_confirmed.is_(False),
            User.created_at >= seuil_suppr_min,
            User.created_at < seuil_suppr_max,
        ).all()

        def _release_slot(user_id: int, kind: str, today_iso: str) -> None:
            """Bug H4 — release le claim Redis quand l'envoi échoue,
            pour autoriser un retry au prochain tick (vs. ancienne
            sémantique où le claim AVANT envoi grillait le slot 25h)."""
            try:
                from redis import Redis
                redis_conn = Redis.from_url(
                    os.getenv("REDIS_URL", "redis://redis:6379/0"),
                    socket_connect_timeout=1,
                )
                redis_conn.delete(f"reminder:{kind}:{user_id}:{today_iso}")
            except Exception:
                pass

        def _send_one(task):
            addr, subject, html, user_id, kind = task
            try:
                sent = send_email_via_brevo_api(to=addr, subject=subject, html_content=html)
                if sent is False:
                    logger.error("send_email returned False user_id=%s — releasing slot", user_id)
                    _release_slot(user_id, kind, today_iso)
                    return
                # Bug H7 — log user_id only, jamais l'email.
                logger.info("📧 Email non-confirmation envoyé user_id=%s", user_id)
            except Exception as e:
                logger.error("❌ Erreur envoi email non-confirmation user_id=%s : %s", user_id, e)
                _release_slot(user_id, kind, today_iso)

        tasks = []
        skipped_dup = 0

        if users_rappel:
            for u in users_rappel:
                if not _claim_reminder_slot(u.id, "rappel", today_iso):
                    skipped_dup += 1
                    continue
                tasks.append((
                    u.email,
                    "Myeline - Confirmation d'inscription",
                    render_template("emails/unconfirmed_user.html", user=u),
                    u.id,
                    "rappel",
                ))

        if users_suppr:
            for u in users_suppr:
                if not _claim_reminder_slot(u.id, "suppr", today_iso):
                    skipped_dup += 1
                    continue
                tasks.append((
                    u.email,
                    "⚠️ Myeline — Votre compte sera supprimé dans 7 jours",
                    render_template(
                        "emails/unconfirmed_deletion_warning.html", user=u
                    ),
                    u.id,
                    "suppr",
                ))

        if skipped_dup:
            logger.info(
                "%d rappel(s) déjà envoyé(s) aujourd'hui — ignorés (idempotence)",
                skipped_dup,
            )

        if not tasks:
            logger.info("Aucun utilisateur à prévenir.")
            return {
                "task": "remind_unconfirmed_users",
                "sent": 0,
                "skipped_duplicate": skipped_dup,
            }

        with ThreadPoolExecutor(max_workers=10) as executor:
            list(executor.map(_send_one, tasks))

    except Exception:
        error_message = traceback.format_exc()
        logger.error(error_message)
        # Bug M20 — anciennement DEV_EMAIL était lu au boot du module et
        # passé à send_email_via_brevo_api(to=None) si absent → API rejette
        # silencieusement et aucune alerte n'arrive aux ops. Fix : lire la
        # variable au moment de l'envoi et logger explicitement quand elle
        # manque, plutôt que de bricoler un envoi qui échoue.
        dev_email = os.getenv("DEV_EMAIL")
        if dev_email:
            send_email_via_brevo_api(
                to=dev_email,
                subject="❌ Échec exécution tâche cron remind_unconfirmed_users",
                html_content=(
                    "<h2>Erreur dans la tâche cron remind_unconfirmed_users</h2>"
                    f"<pre>{error_message}</pre>"
                ),
            )
        else:
            logger.error(
                "DEV_EMAIL non configuré — l'alerte cron n'a pas été envoyée par mail."
            )
        return {"task": "remind_unconfirmed_users", "error": "see logs"}

    logger.info("✅ Tâche terminée avec succès")
    return {
        "task": "remind_unconfirmed_users",
        "sent": len(tasks),
        "skipped_duplicate": skipped_dup,
    }
