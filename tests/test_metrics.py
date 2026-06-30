"""Tests for app.utils.metrics — Prometheus exposition + business gauges."""
from unittest.mock import patch


class TestMetricsRender:
    def test_render_returns_prometheus_format(self, app):
        from app.utils.metrics import render_metrics
        with app.app_context():
            body, ctype = render_metrics()
        assert isinstance(body, (bytes, bytearray))
        assert "text/plain" in ctype or "openmetrics" in ctype

    def test_render_contains_business_metrics(self, app):
        from app.utils.metrics import render_metrics
        with app.app_context():
            body, _ = render_metrics()
        text = body.decode("utf-8", errors="replace")
        # All app-level metric names must appear in the exposition.
        for name in (
            "myeline_http_requests_total",
            "myeline_http_request_duration_seconds",
            "myeline_rag_queries_total",
            "myeline_audit_events_total",
            "myeline_users_total",
            "myeline_conversations_total",
        ):
            assert name in text, f"{name} missing from /metrics output"


class TestRefreshBusinessGauges:
    def test_runs_without_error(self, app):
        from app.utils.metrics import refresh_business_gauges
        with app.app_context():
            # Smoke test: the function must not raise even with empty tables.
            refresh_business_gauges()

    def test_swallows_db_errors(self, app):
        """refresh_business_gauges must not crash when the DB is unreachable."""
        from app.utils.metrics import refresh_business_gauges
        with app.app_context():
            with patch("app.ext.db.session.query", side_effect=Exception("db down")):
                # Should not raise.
                refresh_business_gauges()


class TestCounterIncrement:
    def test_rag_queries_total_increments(self, app):
        from app.utils.metrics import rag_queries_total
        before = rag_queries_total.labels(scope="shared", outcome="success")._value.get()
        rag_queries_total.labels(scope="shared", outcome="success").inc()
        after = rag_queries_total.labels(scope="shared", outcome="success")._value.get()
        assert after == before + 1

    def test_audit_events_total_increments(self, app):
        from app.utils.metrics import audit_events_total
        before = audit_events_total.labels(action="test.metric")._value.get()
        audit_events_total.labels(action="test.metric").inc()
        assert audit_events_total.labels(action="test.metric")._value.get() == before + 1


class TestMetricsEndpoint:
    def test_endpoint_returns_200(self, client):
        r = client.get("/metrics")
        assert r.status_code == 200
        assert b"myeline_" in r.data

    def test_healthz_always_200(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.get_json()["status"] == "alive"
