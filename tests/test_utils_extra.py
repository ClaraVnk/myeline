"""Tests for utils: mailer, recaptcha, flask_limiter, logger."""
import gzip
import logging
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest


# ── mailer ────────────────────────────────────────────────────────────────────

@pytest.mark.no_mock_email
class TestMailer:
    """Exercises send_email_via_brevo_api directly — must opt out of the
    global autouse mailer mock and force-enable real delivery from the
    test FLASK_ENV (which defaults to "testing", normally a kill-switch)."""

    def _force_real_delivery(self, app):
        """Set the kill-switch escape so the mailer actually reaches requests.post."""
        app.config["MAIL_FORCE_SEND"] = "true"
        app.config["MAIL_DRY_RUN"] = "false"

    def test_send_skipped_for_dummy_key(self, app):
        from app.utils.mailer import send_email_via_brevo_api
        with app.app_context():
            app.config["BREVO_API_KEY"] = "dummy"
            result = send_email_via_brevo_api("test@example.com", "Subject", "<p>body</p>")
            assert result is True

    def test_send_raises_without_key(self, app):
        from app.utils.mailer import send_email_via_brevo_api
        with app.app_context():
            app.config["BREVO_API_KEY"] = ""
            self._force_real_delivery(app)
            with pytest.raises(RuntimeError):
                send_email_via_brevo_api("test@example.com", "Subject", "<p>body</p>")

    @patch("app.utils.mailer.requests.post")
    def test_send_success(self, mock_post, app):
        from app.utils.mailer import send_email_via_brevo_api
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"messageId": "abc123"}
        mock_post.return_value = mock_response

        with app.app_context():
            app.config["BREVO_API_KEY"] = "real-key-for-test"
            self._force_real_delivery(app)
            result = send_email_via_brevo_api("test@example.com", "Subject", "<p>body</p>")
            assert result == {"messageId": "abc123"}

    @patch("app.utils.mailer.requests.post")
    def test_send_request_error_returns_false(self, mock_post, app):
        import requests

        from app.utils.mailer import send_email_via_brevo_api
        mock_post.side_effect = requests.RequestException("network error")

        with app.app_context():
            app.config["BREVO_API_KEY"] = "real-key-for-test"
            self._force_real_delivery(app)
            result = send_email_via_brevo_api("test@example.com", "Subject", "<p>body</p>")
            assert result is False


# ── recaptcha ─────────────────────────────────────────────────────────────────

class TestVerifyRecaptcha:
    def test_disabled_returns_true(self, app):
        from app.utils.recaptcha import verify_recaptcha
        with app.test_request_context():
            assert verify_recaptcha("any-token", "login") is True

    def test_production_path_success(self, app):
        """Production recaptcha path: success=True, score>=0.5, action match."""
        from app.utils.recaptcha import verify_recaptcha

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "success": True,
            "score": 0.9,
            "action": "login",
        }
        with app.test_request_context():
            app.config["RECAPTCHA_SECRET_KEY"] = "test-secret"
            with patch.dict(
                os.environ,
                {"FLASK_ENV": "production", "DISABLE_RECAPTCHA": "false"},
            ), patch("requests.post", return_value=mock_resp):
                result = verify_recaptcha("token", "login")
        assert result is True

    def test_production_path_failure(self, app):
        """Production recaptcha path: success=False → returns False."""
        from app.utils.recaptcha import verify_recaptcha

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"success": False, "score": 0.1, "action": "login"}
        with app.test_request_context():
            app.config["RECAPTCHA_SECRET_KEY"] = "test-secret"
            with patch.dict(
                os.environ,
                {"FLASK_ENV": "production", "DISABLE_RECAPTCHA": "false"},
            ), patch("requests.post", return_value=mock_resp):
                result = verify_recaptcha("bad-token", "login")
        assert result is False


# ── flask_limiter extras ───────────────────────────────────────────────────────


def test_get_real_ip_with_header(app):
    """get_real_ip returns stripped X-Real-IP when present."""
    from app.utils.flask_limiter import get_real_ip

    with app.test_request_context(headers={"X-Real-IP": "  1.2.3.4  "}):
        ip = get_real_ip()
    assert ip == "1.2.3.4"


def test_get_real_ip_fallback(app):
    """get_real_ip falls back when X-Real-IP absent."""
    from app.utils.flask_limiter import get_real_ip

    with app.test_request_context():
        ip = get_real_ip()
    assert ip is not None


def test_mock_limiter_methods():
    """MockLimiter enabled/init_app/limit/exempt all work without error."""
    from app.utils.flask_limiter import limiter

    assert limiter.enabled() is False
    limiter.init_app(MagicMock())

    def dummy():
        return "ok"

    wrapped = limiter.limit("10 per minute")(dummy)
    assert wrapped is dummy
    assert limiter.exempt(dummy) is dummy


def test_conditional_limit_non_testing(app):
    """conditional_limit falls through to limiter.limit when TESTING not set."""
    from app.utils import flask_limiter as fl_mod

    def dummy():
        return "ok"

    env_without_testing = {k: v for k, v in os.environ.items() if k != "TESTING"}
    with patch.dict(os.environ, env_without_testing, clear=True):
        dec = fl_mod.conditional_limit("5 per minute")
        result = dec(dummy)
    assert result is dummy


# ── logger extras ──────────────────────────────────────────────────────────────


def test_set_level_logger_returns_info():
    """set_level_logger returns INFO for non-development envs."""
    from app.utils.logger import set_level_logger

    assert set_level_logger("production") == logging.INFO
    assert set_level_logger("staging") == logging.INFO


def test_null_handler_emit():
    """NullHandler.emit() silently discards records."""
    from app.utils.logger import NullHandler

    handler = NullHandler()
    record = logging.LogRecord("test", logging.ERROR, "", 0, "msg", (), None)
    handler.emit(record)


def test_gzip_rotating_filename():
    """rotation_filename appends .gz suffix."""
    from app.utils.logger import GzipRotatingFileHandler

    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
        path = f.name
    try:
        h = GzipRotatingFileHandler(path, maxBytes=1000, backupCount=1)
        assert h.rotation_filename("app.log.1") == "app.log.1.gz"
        h.close()
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_gzip_rotating_rotate():
    """rotate() compresses source into gzip dest and removes source."""
    from app.utils.logger import GzipRotatingFileHandler

    with tempfile.NamedTemporaryFile(delete=False) as src:
        src.write(b"log data here")
        src_path = src.name

    dst_path = src_path + ".gz"
    handler_log = src_path + ".h.log"
    try:
        h = GzipRotatingFileHandler(handler_log, maxBytes=1000, backupCount=1)
        h.rotate(src_path, dst_path)
        assert not os.path.exists(src_path)
        with gzip.open(dst_path, "rb") as gf:
            assert gf.read() == b"log data here"
        h.close()
    finally:
        for p in (dst_path, handler_log):
            if os.path.exists(p):
                os.unlink(p)


def test_get_logger_log_to_stdout():
    """get_logger with LOG_TO_STDOUT=true adds a StreamHandler."""
    from app.utils.logger import get_logger

    name = "test_stdout_unique_abc123"
    lg = logging.getLogger(name)
    for h in lg.handlers[:]:
        lg.removeHandler(h)

    with patch.dict(os.environ, {"LOG_TO_STDOUT": "true"}):
        logger = get_logger(name)

    assert any(isinstance(h, logging.StreamHandler) for h in logger.handlers)
    for h in logger.handlers[:]:
        logger.removeHandler(h)


def test_get_logger_exception_fallback():
    """get_logger falls back to StreamHandler when log directory creation fails."""
    import logging as _logging

    from app.utils.logger import get_logger

    name = "test_exc_fallback_unique_xyz789"
    lg = _logging.getLogger(name)
    for h in lg.handlers[:]:
        lg.removeHandler(h)

    with patch.dict(os.environ, {"LOG_TO_STDOUT": "false"}), \
         patch("pathlib.Path.mkdir", side_effect=OSError("no space")):
        logger = get_logger(name)

    assert logger is not None
    assert any(isinstance(h, _logging.StreamHandler) for h in logger.handlers)
    for h in logger.handlers[:]:
        logger.removeHandler(h)


# ── mailer attachment ──────────────────────────────────────────────────────────


@pytest.mark.no_mock_email
def test_send_email_with_attachment(app):
    """send_email_via_brevo_api includes base64-encoded attachment in payload."""
    from app.utils.mailer import send_email_via_brevo_api

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(b"PDF content here")
        tmp_path = f.name

    try:
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"messageId": "ok"}
        with app.app_context():
            app.config["BREVO_API_KEY"] = "real-key-for-test"
            app.config["MAIL_FORCE_SEND"] = "true"
            app.config["MAIL_DRY_RUN"] = "false"
            with patch("requests.post", return_value=mock_resp) as mock_post:
                send_email_via_brevo_api(
                    to="test@example.com",
                    subject="With attachment",
                    html_content="<p>Body</p>",
                    attachment_path=tmp_path,
                )
        payload = mock_post.call_args[1]["json"]
        assert "attachment" in payload
        assert payload["attachment"][0]["name"].endswith(".pdf")
    finally:
        os.unlink(tmp_path)
