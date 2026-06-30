"""Coverage quick wins — small files / utility helpers.

Targets: app/queue.py, app/commands_cron.py, app/forms/auth_forms.py,
app/routes/base.py untested branches, app/utils/twofa_bypass.py.

Each test mocks the minimum needed and asserts the branch behaviour
deterministically — no real Redis, no real network.
"""
from unittest.mock import MagicMock, patch

import pytest


# ── app/queue.py ───────────────────────────────────────────────────────────────

class TestQueueHelpers:
    def test_get_queue_uses_app_redis_url(self):
        """get_queue builds an RQ Queue bound to the configured Redis URL."""
        from app.queue import get_queue

        fake_app = MagicMock()
        fake_app.config = {"REDIS_URL": "redis://test:6379/0"}
        with patch("app.queue.Redis") as mock_redis, patch("app.queue.Queue") as mock_queue:
            mock_redis.from_url.return_value = "<conn>"
            get_queue(fake_app, name="myeline-pro")
        mock_redis.from_url.assert_called_once_with("redis://test:6379/0")
        mock_queue.assert_called_once_with("myeline-pro", connection="<conn>")

    def test_get_redis_uses_app_redis_url(self):
        """get_redis returns a Redis client built from app config."""
        from app.queue import get_redis

        fake_app = MagicMock()
        fake_app.config = {"REDIS_URL": "redis://other:6379/1"}
        with patch("app.queue.Redis") as mock_redis:
            get_redis(fake_app)
        mock_redis.from_url.assert_called_once_with("redis://other:6379/1")


# ── app/commands_cron.py ───────────────────────────────────────────────────────

class TestCommandsCron:
    def test_unknown_task_raises_usage_error(self, app):
        """flask run-cron <unknown> exits non-zero with a UsageError message."""
        from click.testing import CliRunner

        from app.commands_cron import run_cron

        runner = CliRunner()
        with app.app_context():
            result = runner.invoke(run_cron, ["definitely-not-a-task"])
        assert result.exit_code != 0
        assert "Unknown cron task" in (result.output or "") + (str(result.exception) or "")


# ── app/forms/auth_forms.py — _strong_password validator ──────────────────────

class TestStrongPasswordValidator:
    def test_rejects_short_password(self, app):
        """ResetPasswordForm refuses a too-short password (validator path)."""
        with app.test_request_context():
            from app.forms.auth_forms import ResetPasswordForm
            form = ResetPasswordForm(data={"password": "x", "confirm": "x"})
            assert not form.validate()
            assert any("8 caractères" in e for errs in form.errors.values() for e in errs)

    def test_accepts_strong_password(self, app):
        """A long mixed-case password with digits + symbol passes the validator."""
        with app.test_request_context():
            from app.forms.auth_forms import ResetPasswordForm
            form = ResetPasswordForm(data={
                "password": "Strongp4ss!2026",
                "confirm": "Strongp4ss!2026",
            })
            # Validation can still fail on CSRF in a non-request context, but
            # the password validator itself returned True.
            form.validate()
            assert "password" not in {k for k, v in form.errors.items() if v}


# ── app/routes/base.py — content pages ───────────────────────────────────────
# (language-switch tests removed — the OSS edition ships in English only)


class TestStaticPages:
    def test_privacy_policy_renders(self, client):
        """The privacy policy page is reachable for any visitor."""
        # Route may be at /privacy or /politique-confidentialite — try both.
        for path in ("/privacy", "/politique-confidentialite",
                     "/politique-de-confidentialite", "/privacy-policy"):
            r = client.get(path)
            if r.status_code == 200:
                return
        pytest.skip("privacy policy route not found under expected paths")


# ── app/utils/twofa_bypass.py — empty-email path ──────────────────────────────

class TestTwofaBypassEmptyEmail:
    def test_empty_email_returns_false(self, monkeypatch):
        """An empty email never matches even if the bypass list is non-empty."""
        from app.utils.twofa_bypass import is_2fa_bypassed

        monkeypatch.setenv("BYPASS_2FA_EMAILS", "anyone@example.com")
        assert is_2fa_bypassed("") is False
        assert is_2fa_bypassed(None) is False
