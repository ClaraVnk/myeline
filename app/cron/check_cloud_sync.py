from datetime import datetime, timedelta, timezone

from flask.cli import AppGroup

from app.utils.logger import get_logger


logger = get_logger("check_cloud_sync", subfolder="cron")

CRON_SCHEDULE = "0 */4 * * *"
CRON_DESCRIPTION = "Sync user cloud connections (hourly floor)"

cron_cli = AppGroup("cron")


# Minimum interval between automatic syncs. The connection's own
# ``sync_frequency_minutes`` (set by the user in the UI) is honoured only
# if it's coarser than this floor — we never pull from a drive more than
# once an hour automatically.
_MIN_FREQUENCY_MINUTES = 60


def _effective_min_minutes(conn) -> int:
    """Resolve the minimum interval between syncs for this connection.

    The cadence is the *coarser* of the floor and the value the user
    picked in the UI — never finer than the floor allows.
    """
    requested = max(1, int(conn.sync_frequency_minutes or 240))
    return max(_MIN_FREQUENCY_MINUTES, requested)


def _is_due(conn) -> bool:
    """Return True if the connection is due for a sync."""
    minutes = _effective_min_minutes(conn)
    if conn.last_sync_at is None:
        return True
    delta = timedelta(minutes=minutes)
    last = conn.last_sync_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last >= delta


@cron_cli.command("check-cloud-sync")
def check_cloud_sync():
    from flask import current_app
    from redis import Redis
    from rq import Queue

    from app.models.cloud_connection import CloudConnection

    logger.info("Starting check_cloud_sync cron")

    connections = (
        CloudConnection.query
        .filter_by(is_syncing=False)
        .all()
    )
    due_ids = [c.id for c in connections if _is_due(c)]

    if not due_ids:
        logger.info("check_cloud_sync: no connections due for sync")
        return

    redis_conn = Redis.from_url(current_app.config["REDIS_URL"])
    q = Queue("myeline", connection=redis_conn)

    # Bug H17 — race condition : deux ticks de cron pouvaient enqueue la
    # même connexion (le flag `is_syncing` n'était mis qu'au démarrage du
    # job par le worker). Fix : lock Redis SETNX par conn_id avec TTL
    # légèrement > job_timeout, qui empêche un autre tick de re-enqueue
    # tant que le job n'a pas eu le temps de tourner. Si le worker meurt
    # avant pickup, le lock expire seul et un cron suivant pourra
    # re-tenter — pas de connexion bloquée indéfiniment.
    enqueued = 0
    for conn_id in due_ids:
        lock_key = f"cloudsync:enqueued:{conn_id}"
        # NX = only set if not exists ; EX 900 = TTL 15 min (job_timeout
        # est 600s, on garde une marge de 5 min pour la queue + retries).
        if redis_conn.set(lock_key, "1", nx=True, ex=900):
            q.enqueue("app.tasks.sync_cloud_connection_task", conn_id, job_timeout=600)
            enqueued += 1
        else:
            logger.debug("check_cloud_sync: conn %s already enqueued recently", conn_id)

    logger.info("check_cloud_sync: enqueued %d connections", enqueued)
