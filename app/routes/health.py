"""Health & readiness endpoints.

Two endpoints, deliberately distinct:

  /healthz  — liveness. "The Python process is up and serving HTTP."
              Always 200 unless the WSGI worker itself is dead. K8s /
              container orchestrators use this to decide whether to
              restart the pod.

  /health   — readiness + dependency report. Walks every external
              dependency, returns 200 if all CRITICAL deps are
              healthy, 503 otherwise. WARNING deps (Ollama / Mistral)
              degrade the readiness signal but don't make us 503 —
              the app is still usable for non-RAG flows when an LLM
              backend is down.
"""
import os

import redis
import requests
from flask import Blueprint, jsonify

from app.ext import db
from app.utils.observability_auth import observability_authorized


health_bp = Blueprint("health", __name__)


_HTTP_TIMEOUT = 2.5  # seconds — short on purpose; healthchecks must be fast


def _check_database():
    try:
        db.session.execute(db.text("SELECT 1"))
        return {"status": "healthy", "message": "Connected"}
    except Exception as e:
        return {"status": "unhealthy", "message": str(e)[:200]}


def _check_redis():
    try:
        url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        r = redis.from_url(url, socket_connect_timeout=_HTTP_TIMEOUT)
        r.ping()
        return {"status": "healthy", "message": "Connected"}
    except Exception as e:
        return {"status": "unhealthy", "message": str(e)[:200]}


def _check_ollama():
    """Optional. Sovereign deployments hard-depend on Ollama; SaaS treats
    it as a fallback (warning, not failure)."""
    url = os.getenv("OLLAMA_URL", "http://ollama:11434")
    try:
        r = requests.get(f"{url}/api/tags", timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        return {"status": "healthy", "message": "responding"}
    except Exception as e:
        # Sovereign mode treats Ollama as critical — surface the failure.
        from app.utils.deployment_mode import is_sovereign
        sev = "unhealthy" if is_sovereign() else "degraded"
        return {"status": sev, "message": str(e)[:200]}


def _check_mistral():
    """Skipped when MISTRAL_API_KEY isn't set OR sovereign mode is on."""
    from app.utils.deployment_mode import is_sovereign
    if is_sovereign():
        return {"status": "skipped", "message": "Sovereign mode — external AI never called"}
    api_key = os.getenv("MISTRAL_API_KEY", "")
    if not api_key:
        return {"status": "skipped", "message": "MISTRAL_API_KEY not configured"}
    try:
        r = requests.get(
            "https://api.mistral.ai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=_HTTP_TIMEOUT,
        )
        if r.status_code == 200:
            return {"status": "healthy", "message": "API reachable"}
        if r.status_code == 401:
            return {"status": "unhealthy", "message": "MISTRAL_API_KEY rejected (401)"}
        return {"status": "degraded", "message": f"Status {r.status_code}"}
    except Exception as e:
        return {"status": "degraded", "message": str(e)[:200]}


@health_bp.route("/healthz")
def liveness():
    """Liveness: process is up. Always 200 unless gunicorn itself is down
    (in which case the response never makes it out)."""
    return jsonify({"status": "alive"}), 200


@health_bp.route("/metrics")
def metrics():
    """Prometheus exposition endpoint.

    Gated by ``observability_authorized()`` (round 5 A4-H1): public
    callers used to be able to scrape business gauges (users per tier,
    enterprise stacks per status, conversation totals). Now requires a
    bearer token (``OBSERVABILITY_API_KEY``) OR a private-network source
    IP.
    """
    if not observability_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    from app.utils.metrics import render_metrics
    body, ctype = render_metrics()
    return body, 200, {"Content-Type": ctype}


@health_bp.route("/health")
def health_check():
    """Readiness + dependency report.

    Critical deps (DB, Redis): if any unhealthy → 503.
    Warning deps (Ollama, Mistral): degrade the report but stay 200,
    EXCEPT Ollama in sovereign mode where it becomes critical.

    Round 5 A4-H2: public callers receive only the aggregate verdict
    (``status`` field) without per-dependency detail or environment /
    container metadata. Authorized callers (bearer or private IP) keep
    the full payload that ops tooling consumes.
    """
    checks = {
        "database": _check_database(),
        "redis": _check_redis(),
        "ollama": _check_ollama(),
        "mistral": _check_mistral(),
    }

    in_container = (
        os.path.exists("/.dockerenv")
        or os.path.exists("/run/.containerenv")
        or os.getenv("DOCKER_CONTAINER") == "true"
    )
    checks["container"] = {
        "status": "running" if in_container else "unknown",
        "message": "Running in container" if in_container else "Not in a container",
    }

    from app.utils.deployment_mode import is_sovereign
    critical = ["database", "redis"]
    if is_sovereign():
        critical.append("ollama")
    unhealthy = [s for s in critical if checks[s]["status"] == "unhealthy"]
    if unhealthy:
        overall = "unhealthy"
        code = 503
    elif any(c.get("status") == "degraded" for c in checks.values()):
        overall = "degraded"
        code = 200
    else:
        overall = "healthy"
        code = 200

    if not observability_authorized():
        return jsonify({"status": overall}), code

    return jsonify({
        "status": overall,
        "environment": os.getenv("FLASK_ENV", "unknown"),
        "version": "1.0.0",
        "checks": checks,
    }), code
