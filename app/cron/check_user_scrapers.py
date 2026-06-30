from flask.cli import AppGroup

from app.utils.logger import get_logger


logger = get_logger("check_user_scrapers", subfolder="cron")

CRON_SCHEDULE = "0 3,7,23 * * *"
CRON_DESCRIPTION = "Refresh personal RSS/web scraper sources (Pro+ only, off-peak)"

cron_cli = AppGroup("cron")


@cron_cli.command("check-user-scrapers")
def check_user_scrapers():
    from flask import current_app
    from redis import Redis
    from rq import Queue

    from app.models.user import User
    from app.models.user_scraper import UserScraper
    from app.services.user_scraper_service import _scraper_is_due

    logger.info("Starting check_user_scrapers cron")

    # Single JOIN query — was N+1 (one SELECT per Pro user). Perf audit H7.
    # Bug M21 — l'ancien filtre `abonnement_type == "premium_pro"` excluait
    # silencieusement Team et Enterprise (qui ont aussi des scrapers via les
    # plans org). Régression fonctionnelle invisible : les comptes les plus
    # chers ne voyaient pas leurs scrapers rafraîchis. Inclusion explicite.
    # OSS self-host: every user's active scrapers run — no subscription gating.
    due_scrapers = (
        UserScraper.query
        .filter(UserScraper.is_active.is_(True))
        .all()
    )

    due_scraper_ids = [s.id for s in due_scrapers if _scraper_is_due(s)]

    if not due_scraper_ids:
        logger.info("check_user_scrapers: no scrapers due")
        return {"task": "check_user_scrapers", "due": 0, "enqueued": 0}

    redis_conn = Redis.from_url(current_app.config["REDIS_URL"])
    q = Queue("myeline", connection=redis_conn)
    # Bug M1 (audit round 2) — pattern identique à check_cloud_sync (H17) :
    # SETNX lock par scraper_id, TTL > job_timeout, pour éviter double
    # enqueue quand deux ticks de cron se chevauchent (restart sidecar,
    # supercronic backlog). Sans, deux fetch parallèles → IntegrityError
    # sur url_hash unique → scraper marqué en erreur.
    enqueued = 0
    for scraper_id in due_scraper_ids:
        lock_key = f"scraper:enqueued:{scraper_id}"
        if redis_conn.set(lock_key, "1", nx=True, ex=600):
            q.enqueue("app.tasks.scrape_source_task", scraper_id, job_timeout=300)
            enqueued += 1
        else:
            logger.debug("check_user_scrapers: scraper %s already enqueued recently", scraper_id)

    logger.info("check_user_scrapers: enqueued %d scrapers (%d due)",
                enqueued, len(due_scraper_ids))
    return {
        "task": "check_user_scrapers",
        "due": len(due_scraper_ids),
        "enqueued": enqueued,
    }
