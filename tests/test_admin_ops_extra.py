"""Tests for admin_ops routes — clear-logs, clear-cache, export-data, delete-log/report."""
from unittest.mock import MagicMock, patch

import pytest

from tests.helpers import admin_login


def test_clear_logs(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.post("/admin/tools/clear-logs")
    assert r.status_code == 200
    assert r.get_json()["success"] is True
    client.get("/admin/logout")


def test_clear_cache_preview_redis_error(client, admin_user):
    """Preview returns 503 when Redis is unreachable."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    with patch(
        "app.routes.admin.admin_ops._redis_client_for_clear",
        side_effect=Exception("no redis"),
    ):
        r = client.get("/admin/tools/clear-cache/preview")
    assert r.status_code == 503
    assert r.get_json()["success"] is False
    client.get("/admin/logout")


def test_clear_cache_requires_confirm_token(client, admin_user):
    """A bare POST without token is refused — no FLUSHDB happens."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    mock_redis = MagicMock()
    with patch(
        "app.routes.admin.admin_ops._redis_client_for_clear",
        return_value=mock_redis,
    ):
        r = client.post("/admin/tools/clear-cache")
    assert r.status_code == 400
    assert r.get_json()["success"] is False
    mock_redis.flushdb.assert_not_called()
    client.get("/admin/logout")


def test_clear_cache_rejects_invalid_token(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    mock_redis = MagicMock()
    with patch(
        "app.routes.admin.admin_ops._redis_client_for_clear",
        return_value=mock_redis,
    ):
        r = client.post(
            "/admin/tools/clear-cache",
            json={"confirm_token": "tampered-token"},
        )
    assert r.status_code == 400
    mock_redis.flushdb.assert_not_called()
    client.get("/admin/logout")


def test_clear_cache_two_step_flow_success(client, admin_user):
    """Preview → POST with the issued token flushes the DB."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    mock_redis = MagicMock()
    mock_redis.dbsize.return_value = 42
    with patch(
        "app.routes.admin.admin_ops._redis_client_for_clear",
        return_value=mock_redis,
    ):
        preview = client.get("/admin/tools/clear-cache/preview")
        assert preview.status_code == 200
        token = preview.get_json()["confirm_token"]
        assert preview.get_json()["dbsize"] == 42

        r = client.post(
            "/admin/tools/clear-cache",
            json={"confirm_token": token},
        )
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["success"] is True
    assert body["keys_flushed"] == 42
    mock_redis.flushdb.assert_called_once()
    client.get("/admin/logout")


def test_clear_cache_rejects_dbsize_drift(client, admin_user):
    """Token issued for dbsize=10 but DB now has 999 → refused (race detected)."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    mock_redis = MagicMock()
    mock_redis.dbsize.return_value = 10
    with patch(
        "app.routes.admin.admin_ops._redis_client_for_clear",
        return_value=mock_redis,
    ):
        preview = client.get("/admin/tools/clear-cache/preview")
        token = preview.get_json()["confirm_token"]
        # Simulate a different key count at confirm time.
        mock_redis.dbsize.return_value = 999
        r = client.post(
            "/admin/tools/clear-cache",
            json={"confirm_token": token},
        )
    assert r.status_code == 409
    mock_redis.flushdb.assert_not_called()
    client.get("/admin/logout")


def test_export_data(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.get("/admin/tools/export-data")
    assert r.status_code == 200
    assert r.content_type == "application/json"
    client.get("/admin/logout")


def test_delete_log_nonexistent(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.delete("/admin/tools/delete-log/nonexistent.log")
    assert r.status_code == 200
    client.get("/admin/logout")


def test_delete_report_nonexistent(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.delete("/admin/tools/delete-report/nonexistent.json")
    assert r.status_code == 200
    client.get("/admin/logout")


def test_restart_app(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    with patch("subprocess.Popen") as mock_popen:
        mock_popen.return_value = MagicMock()
        r = client.post("/admin/tools/restart")
    assert r.status_code == 200
    client.get("/admin/logout")
