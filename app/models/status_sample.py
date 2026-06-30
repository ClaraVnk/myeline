"""Status samples — one row every 5 min recording dependency state.

Drives the public /status page (current state + 30-day uptime bars).
Kept simple: one boolean per dependency, no separate latency table.
"""
from app.ext import db
from app.utils.time_utils import utcnow


class StatusSample(db.Model):
    __tablename__ = "status_samples"

    id = db.Column(db.Integer, primary_key=True)
    sampled_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
    # status enum encoded as short strings to avoid migration if a new
    # state appears (e.g. "throttled"). Constrained at the app layer.
    db_status = db.Column(db.String(16), nullable=False, default="unknown")
    redis_status = db.Column(db.String(16), nullable=False, default="unknown")
    ollama_status = db.Column(db.String(16), nullable=False, default="unknown")
    mistral_status = db.Column(db.String(16), nullable=False, default="unknown")
    web_status = db.Column(db.String(16), nullable=False, default="unknown")
    # Optional latency snapshot (ms) — None when probe didn't measure.
    web_response_ms = db.Column(db.Integer, nullable=True)
