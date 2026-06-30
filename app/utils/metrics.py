"""Prometheus metrics exposed under /metrics.

Two layers:
  - Generic HTTP layer (request counter + duration histogram) wired
    via Flask before/after request hooks.
  - Application counters / gauges incremented by business code (RAG
    queries, audit events, broadcasts). Modules import the symbols
    they need and call .inc() / .set() — keeps the metrics surface
    inspectable in one file.

The collector is module-level (the prometheus_client default registry)
so metrics persist across requests without us building our own
storage layer. Multi-process gunicorn requires the multiprocess mode
(`PROMETHEUS_MULTIPROC_DIR`); we'll add that if/when we move past a
single worker.
"""
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest


# ─── HTTP layer ────────────────────────────────────────────────────────────
http_requests_total = Counter(
    "myeline_http_requests_total",
    "Total HTTP requests handled by the app, labelled by method, endpoint and status code.",
    labelnames=("method", "endpoint", "status"),
)

http_request_duration_seconds = Histogram(
    "myeline_http_request_duration_seconds",
    "End-to-end HTTP request latency in seconds.",
    labelnames=("method", "endpoint"),
    buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)


# ─── Business counters / gauges ────────────────────────────────────────────
rag_queries_total = Counter(
    "myeline_rag_queries_total",
    "RAG search queries served, labelled by scope and outcome.",
    labelnames=("scope", "outcome"),  # outcome: success | error | empty
)

broadcast_emails_total = Counter(
    "myeline_broadcast_emails_total",
    "Broadcast emails enqueued (NOT individual sends — each enqueue covers N recipients).",
    labelnames=("plans",),
)

audit_events_total = Counter(
    "myeline_audit_events_total",
    "Audit-log entries written, labelled by action.",
    labelnames=("action",),
)

# Gauges populated by the metrics endpoint itself (each scrape recomputes).
users_total = Gauge(
    "myeline_users_total",
    "Active confirmed users.",
)

conversations_total = Gauge(
    "myeline_conversations_total",
    "Total RAG conversations across all users.",
)


def refresh_business_gauges():
    """Pull live values for the gauges from the DB.

    Called from the /metrics route so each scrape is fresh. Exception-safe
    — if the DB is down, gauges keep their last value rather than zeroing
    out (zero is misleading; staleness is honest).
    """
    try:
        from app.ext import db
        from app.models.conversation import Conversation
        from app.models.user import User

        count = (
            db.session.query(User)
            .filter(User.is_active.is_(True), User.is_confirmed.is_(True))
            .count()
        )
        users_total.set(count)

        conversations_total.set(db.session.query(Conversation).count())
    except Exception:
        # Honest staleness — see docstring.
        pass


def render_metrics() -> tuple:
    """Return (body, content_type) for the /metrics route."""
    refresh_business_gauges()
    return generate_latest(), CONTENT_TYPE_LATEST
