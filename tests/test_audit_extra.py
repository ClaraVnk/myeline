"""Coverage tests for app.utils.audit — non-JSON payloads, missing actor, write failure."""
from unittest.mock import MagicMock, patch

from app.utils.audit import log_audit


def test_log_audit_with_unauthenticated_current_user(app, monkeypatch):
    """If current_user.is_authenticated is False, actor stays None and entry uses 'system'."""
    fake_user = MagicMock()
    fake_user.is_authenticated = False

    with app.app_context():
        added = []
        flushed = []
        with patch("flask_login.current_user", fake_user), \
             patch("app.ext.db.session.add", side_effect=added.append), \
             patch("app.ext.db.session.flush", side_effect=lambda: flushed.append(1)):
            log_audit("test.action")
        # add was called with a single AuditLog whose email defaulted to "system".
        assert len(added) == 1
        assert added[0].actor_email == "system"


def test_log_audit_handles_request_context_failures(app, monkeypatch):
    """No request context → ip/ua remain None without raising."""
    with app.app_context():
        added = []
        with patch("app.ext.db.session.add", side_effect=added.append), \
             patch("app.ext.db.session.flush"):
            log_audit("cli.action", actor=None)
        assert added and added[0].ip_address is None and added[0].user_agent is None


def test_log_audit_payload_not_json_serialisable(app):
    """Non-JSON-safe payload values are coerced via str(...) — entry still written."""
    class Weird:
        def __repr__(self):
            return "<weird>"

    with app.app_context():
        added = []
        with patch("app.ext.db.session.add", side_effect=added.append), \
             patch("app.ext.db.session.flush"):
            log_audit("x", payload={"obj": Weird()})
        assert added and added[0].payload_json is not None
        assert "<weird>" in added[0].payload_json


def test_log_audit_payload_serialisation_failure_logs_warning(app):
    """If json.dumps explodes, payload_str is None and a warning is logged."""
    with app.app_context():
        added = []
        with patch("app.ext.db.session.add", side_effect=added.append), \
             patch("app.ext.db.session.flush"), \
             patch("app.utils.audit.json.dumps", side_effect=TypeError("bad")), \
             patch("app.utils.audit.logger.warning") as warn:
            log_audit("x", payload={"k": "v"})
        assert added and added[0].payload_json is None
        warn.assert_called_once()


def test_log_audit_db_failure_swallowed(app):
    """A DB write failure is logged at warning, never raised."""
    with app.app_context():
        with patch("app.ext.db.session.add", side_effect=RuntimeError("db down")), \
             patch("app.utils.audit.logger.warning") as warn:
            # Must not raise.
            log_audit("x", actor=None)
        warn.assert_called_once()


def test_log_audit_truncates_long_action_and_target(app):
    """Action > 80 chars and target_id > 80 chars are truncated to fit columns."""
    long_action = "a" * 200
    long_target = "t" * 200
    with app.app_context():
        added = []
        with patch("app.ext.db.session.add", side_effect=added.append), \
             patch("app.ext.db.session.flush"):
            log_audit(long_action, target_type="organization" * 10,
                      target_id=long_target, payload=None)
        entry = added[0]
        assert len(entry.action) == 80
        assert len(entry.target_id) == 80
        assert len(entry.target_type) == 40


def test_log_audit_metrics_failure_swallowed(app):
    """If metrics.inc() blows up, the audit row is still committed."""
    with app.app_context():
        added = []
        with patch("app.ext.db.session.add", side_effect=added.append), \
             patch("app.ext.db.session.flush"), \
             patch("app.utils.metrics.audit_events_total") as metric:
            metric.labels.side_effect = RuntimeError("prom borked")
            log_audit("x", actor=None)
        assert len(added) == 1
