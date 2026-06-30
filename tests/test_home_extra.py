"""Coverage for app/routes/home.py — pricing/status pages and helpers
not exercised by existing test_home.py (which only checks the welcome
page).
"""
from datetime import datetime, timedelta, timezone


# ─── /status renders ──────────────────────────────────────────────────

class TestStatusPage:
    def test_renders_with_no_samples(self, client, app):
        r = client.get("/status")
        assert r.status_code == 200

    def test_renders_with_recent_samples(self, app, db, client):
        from app.models.status_sample import StatusSample
        with app.app_context():
            db.session.add(StatusSample(
                sampled_at=datetime.now(timezone.utc),
                db_status="ok", redis_status="ok",
                ollama_status="ok", mistral_status="skipped",
                web_status="ok",
            ))
            db.session.add(StatusSample(
                sampled_at=datetime.now(timezone.utc) - timedelta(days=1),
                db_status="degraded", redis_status="ok",
                ollama_status="error", mistral_status="ok",
                web_status="ok",
            ))
            db.session.commit()
        r = client.get("/status")
        assert r.status_code == 200
