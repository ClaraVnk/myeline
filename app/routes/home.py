from flask import Blueprint, make_response, render_template, request

from app.utils.logger import get_logger


logger = get_logger("home", subfolder="routes")

home_bp = Blueprint("home", __name__)


@home_bp.route("/welcome")
def welcome_page():
    logger.info("Page d'accueil affichée.")
    return render_template("base/home.html")


@home_bp.route("/status")
def status_page():
    """Public status page — current state + 30-day uptime bars per service.

    Reads from the status_samples table populated every 5 min by the
    record_status cron. No auth — this is the page customers paste in
    incident channels.
    """
    from datetime import datetime, timedelta, timezone

    from app.ext import db
    from app.models.status_sample import StatusSample

    # Most recent sample for the "current" panel.
    current = (
        db.session.query(StatusSample)
        .order_by(StatusSample.sampled_at.desc())
        .first()
    )

    # 30-day daily roll-up for the historical bars.
    since = datetime.now(timezone.utc) - timedelta(days=30)
    samples = (
        db.session.query(StatusSample)
        .filter(StatusSample.sampled_at >= since)
        .order_by(StatusSample.sampled_at.asc())
        .all()
    )

    # Bucket by date → {service: count_ok / count_total}.
    services = ("db", "redis", "ollama", "mistral", "web")
    buckets: dict = {}
    for s in samples:
        day = s.sampled_at.date().isoformat()
        bucket = buckets.setdefault(day, {svc: [0, 0] for svc in services})
        for svc in services:
            status = getattr(s, f"{svc}_status", "")
            if status == "skipped":
                continue
            bucket[svc][1] += 1
            if status == "ok":
                bucket[svc][0] += 1

    history = []
    today = datetime.now(timezone.utc).date()
    for i in range(29, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        row = {"date": d, "services": {}}
        b = buckets.get(d)
        for svc in services:
            if not b or b[svc][1] == 0:
                row["services"][svc] = {"uptime": None, "samples": 0}
            else:
                ok, total = b[svc]
                row["services"][svc] = {
                    "uptime": round(ok / total * 100, 1),
                    "samples": total,
                }
        history.append(row)

    # A4-L1: bucket the end-to-end latency before rendering so the page
    # gives operators a "fast / normal / slow / degraded" signal instead
    # of an attacker-useful millisecond reading.
    latency_bucket = None
    if current and current.web_response_ms:
        ms = current.web_response_ms
        if ms < 100:
            latency_bucket = "< 100 ms"
        elif ms < 250:
            latency_bucket = "< 250 ms"
        elif ms < 500:
            latency_bucket = "< 500 ms"
        elif ms < 1000:
            latency_bucket = "< 1 s"
        else:
            latency_bucket = "> 1 s"

    return render_template(
        "base/status.html",
        current=current,
        history=history,
        services=services,
        latency_bucket=latency_bucket,
    )


# Bug M15 — endpoints GET state-changing sans CSRF, secure=False : un
# préfetch ou <img src=> tiers pouvait déclencher le consentement à la
# place de l'utilisateur. Désormais : POST uniquement (CSRF actif via
# Flask-WTF), secure conditionné à HTTPS réel (request.is_secure honore
# X-Forwarded-Proto sous ProxyFix).
def _set_consent_cookie(value: str):
    resp = make_response("", 204)
    resp.set_cookie(
        "cookie_consent",
        value,
        max_age=60 * 60 * 24 * 365,  # 12 mois (conforme RGPD - max 13 mois)
        secure=request.is_secure,
        httponly=True,
        samesite="Lax",
    )
    return resp


@home_bp.route("/reject-cookies", methods=["POST"])
def reject_cookies():
    """Reject non-essential cookies (Google Analytics)."""
    logger.info("Cookie de consentement défini sur 'rejected'.")
    return _set_consent_cookie("rejected")


@home_bp.route("/accept-cookies", methods=["POST"])
def accept_cookies():
    """Accept all cookies including Google Analytics."""
    logger.info("Cookie de consentement défini sur 'accepted'.")
    return _set_consent_cookie("accepted")
