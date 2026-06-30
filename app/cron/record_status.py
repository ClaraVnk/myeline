"""Cron: record_status
Schedule: every 5 minutes
Purpose: sample dependency state into status_samples — feeds the
         public /status page (current state + 30-day uptime bars).

Each sample records db / redis / ollama / mistral / web state, plus
a web response time in ms when measurable.
"""
import os
import time
from datetime import datetime, timedelta, timezone

import requests

from app.utils.logger import get_logger


CRON_SCHEDULE = "*/5 * * * *"
CRON_DESCRIPTION = "Sample dependency state every 5 minutes"

logger = get_logger("record_status", subfolder="cron")

_TIMEOUT = 2.5  # seconds — same as /health probes


def _check_db():
    try:
        from app.ext import db
        db.session.execute(db.text("SELECT 1"))
        return "ok"
    except Exception:
        return "down"


def _check_redis():
    try:
        from redis import Redis
        url = os.getenv("REDIS_URL", "redis://redis:6379/0")
        r = Redis.from_url(url, socket_connect_timeout=_TIMEOUT)
        r.ping()
        return "ok"
    except Exception:
        return "down"


def _check_ollama():
    url = os.getenv("OLLAMA_URL", "http://ollama:11434")
    try:
        r = requests.get(f"{url}/api/tags", timeout=_TIMEOUT)
        return "ok" if r.status_code == 200 else "degraded"
    except Exception:
        return "down"


def _check_mistral():
    # Sovereign deployments must never reach out to api.mistral.ai —
    # the whole point is air-gappable. Treat as skipped regardless of
    # whether a stale key is left in env.
    from app.utils.deployment_mode import is_sovereign
    if is_sovereign():
        return "skipped"
    api_key = os.getenv("MISTRAL_API_KEY", "")
    if not api_key:
        return "skipped"
    try:
        r = requests.get(
            "https://api.mistral.ai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=_TIMEOUT,
        )
        return "ok" if r.status_code == 200 else "degraded"
    except Exception:
        return "down"


def _check_web_self():
    """Probe the web service through HTTP to measure end-to-end response time.

    Returns (status, response_ms). The cron sidecar runs in its own
    container, so we need the compose service name (`web`) instead of
    localhost. ``WEB_INTERNAL_URL`` env var lets prod override it
    (e.g. when reverse-proxied behind a Pangolin tunnel) and tests
    can point it elsewhere. Falls back to localhost so a single-process
    invocation (`flask run-cron record_status` from the web container)
    still works.
    """
    import os
    url = os.getenv("WEB_INTERNAL_URL", "http://web:5000") + "/healthz"
    try:
        start = time.monotonic()
        r = requests.get(url, timeout=_TIMEOUT)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        if r.status_code == 200:
            return "ok", elapsed_ms
        return "degraded", elapsed_ms
    except Exception:
        # Fall back to localhost — covers the case where the cron is run
        # interactively from inside the web container.
        try:
            start = time.monotonic()
            r = requests.get("http://localhost:5000/healthz", timeout=_TIMEOUT)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            if r.status_code == 200:
                return "ok", elapsed_ms
            return "degraded", elapsed_ms
        except Exception:
            return "down", None


def _purge_old_samples(retention_days: int = 30) -> int:
    """Drop status samples older than retention_days. Keeps the
    table from growing unbounded."""
    try:
        from app.ext import db
        from app.models.status_sample import StatusSample

        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        deleted = (
            db.session.query(StatusSample)
            .filter(StatusSample.sampled_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.session.commit()
        return deleted
    except Exception as exc:
        logger.warning("status sample purge failed: %s", exc)
        return 0


def run() -> dict:
    # Bug L13 — l'ancien run() supposait un app_context actif (cas Flask
    # CLI). Lancé en standalone (test, script ops), db.session.execute()
    # plantait en RuntimeError. Fix : si pas de contexte courant, on en
    # crée un via create_app() le temps de l'exécution.
    from flask import has_app_context

    from app.ext import db
    from app.models.status_sample import StatusSample
    from app.utils.time_utils import utcnow

    if not has_app_context():
        from app import create_app
        with create_app().app_context():
            return run()

    web_status, web_ms = _check_web_self()
    sample = StatusSample(
        sampled_at=utcnow(),
        db_status=_check_db(),
        redis_status=_check_redis(),
        ollama_status=_check_ollama(),
        mistral_status=_check_mistral(),
        web_status=web_status,
        web_response_ms=web_ms,
    )
    try:
        db.session.add(sample)
        db.session.commit()
    except Exception as exc:
        logger.error("status sample write failed: %s", exc)
        return {"recorded": False}

    purged = _purge_old_samples()
    if purged:
        logger.info("Purged %d old status samples", purged)
    return {
        "recorded": True,
        "purged": purged,
        "states": {
            "db": sample.db_status, "redis": sample.redis_status,
            "ollama": sample.ollama_status, "mistral": sample.mistral_status,
            "web": sample.web_status, "web_ms": sample.web_response_ms,
        },
    }
