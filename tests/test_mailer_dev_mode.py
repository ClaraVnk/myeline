"""Tests for send_email_via_brevo_api() kill-switch behaviour.

Three switches in precedence order:
  1. MAIL_DRY_RUN=true   → always log, never POST (regardless of FLASK_ENV)
  2. BREVO_API_KEY=dummy → always log
  3. FLASK_ENV != production → log unless MAIL_FORCE_SEND=true
"""
from unittest.mock import MagicMock, patch

import pytest


# Every test in this module exercises send_email_via_brevo_api itself.
# The global autouse mock from conftest.py would short-circuit them, so
# we opt out at the module level.
pytestmark = pytest.mark.no_mock_email


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _call_send(to="test@example.com", subject="Test", html="<b>Hello</b>", **cfg_overrides):
    """Call send_email_via_brevo_api with Flask config patched via _get_cfg.

    Returns the requests.post mock so callers can assert `.called`.
    """
    from app.utils.mailer import send_email_via_brevo_api

    defaults = {
        "BREVO_API_KEY": "sk-test-key",
        "FLASK_ENV": "development",
        "MAIL_DRY_RUN": "false",
        "MAIL_FORCE_SEND": "false",
        "MAIL_DEFAULT_SENDER": "hello@myeline.io",
        "MAIL_SENDER_NAME": "Myeline",
    }
    defaults.update(cfg_overrides)

    def fake_get_cfg(key, default=None):
        return defaults.get(key, default)

    with patch("app.utils.mailer._get_cfg", side_effect=fake_get_cfg):
        with patch("app.utils.mailer.requests.post") as mock_post:
            mock_response = MagicMock()
            mock_response.raise_for_status.return_value = None
            mock_response.json.return_value = {"messageId": "test-id"}
            mock_post.return_value = mock_response
            send_email_via_brevo_api(to=to, subject=subject, html_content=html)
    return mock_post


# ---------------------------------------------------------------------------
# Kill-switch behaviour
# ---------------------------------------------------------------------------

class TestMailerKillSwitch:
    def test_dummy_api_key_skips_http_call(self):
        mock_post = _call_send(BREVO_API_KEY="dummy")
        assert not mock_post.called

    def test_dummy_api_key_calls_logger(self):
        with patch("app.utils.mailer.logger") as mock_logger:
            _call_send(BREVO_API_KEY="dummy", to="bob@example.com", subject="Hello Bob")
        assert mock_logger.info.called

    def test_dry_run_true_skips_http_call(self):
        mock_post = _call_send(FLASK_ENV="production", MAIL_DRY_RUN="true")
        assert not mock_post.called

    def test_dry_run_true_overrides_production(self):
        """Dry-run wins even when FLASK_ENV=production — that's the whole point."""
        with patch("app.utils.mailer.logger") as mock_logger:
            mock_post = _call_send(
                FLASK_ENV="production",
                MAIL_DRY_RUN="true",
                MAIL_FORCE_SEND="true",
                to="carol@example.com",
            )
        assert not mock_post.called
        assert mock_logger.warning.called  # explicit warning that the switch fired

    def test_non_production_logs_by_default(self):
        """FLASK_ENV != production logs without needing MAIL_DRY_RUN."""
        mock_post = _call_send(FLASK_ENV="development")
        assert not mock_post.called

    def test_force_send_re_enables_non_production_delivery(self):
        """Staging that needs real delivery sets MAIL_FORCE_SEND=true."""
        mock_post = _call_send(FLASK_ENV="staging", MAIL_FORCE_SEND="true")
        assert mock_post.called

    def test_production_sends_via_brevo(self):
        mock_post = _call_send(FLASK_ENV="production")
        assert mock_post.called

    def test_missing_api_key_raises_runtime_error(self):
        from app.utils.mailer import send_email_via_brevo_api

        def fake_get_cfg(key, default=None):
            return {"FLASK_ENV": "production", "MAIL_DRY_RUN": "false"}.get(key, default)

        with patch("app.utils.mailer._get_cfg", side_effect=fake_get_cfg):
            with pytest.raises(RuntimeError, match="BREVO_API_KEY"):
                send_email_via_brevo_api(
                    to="x@example.com", subject="S", html_content="<p>H</p>"
                )


# ---------------------------------------------------------------------------
# _dev_log_email
# ---------------------------------------------------------------------------

class TestDevLogEmail:
    def test_strips_html_tags_from_logged_body(self):
        """The text logged by _dev_log_email must not contain HTML tags."""
        from app.utils.mailer import _dev_log_email

        with patch("app.utils.mailer.logger") as mock_logger:
            _dev_log_email("a@b.com", "Subj", "<p>Hello <b>world</b></p>")

        assert mock_logger.info.called
        call_args = mock_logger.info.call_args[0]
        # call_args = (format_str, to, subject, text)
        logged_text = call_args[3]
        assert "<p>" not in logged_text
        assert "<b>" not in logged_text
        assert "Hello" in logged_text
        assert "world" in logged_text

    def test_includes_recipient_and_subject(self):
        """_dev_log_email passes recipient and subject to the logger."""
        from app.utils.mailer import _dev_log_email

        with patch("app.utils.mailer.logger") as mock_logger:
            _dev_log_email("recipient@example.com", "My Subject", "<p>body</p>")

        call_args = mock_logger.info.call_args[0]
        assert "recipient@example.com" in call_args
        assert "My Subject" in call_args
