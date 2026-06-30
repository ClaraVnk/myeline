"""
Extra admin route tests — cron, ops, manage_users, review queue.
"""
import json
from unittest.mock import patch

from tests.helpers import admin_login


# ── Admin cron endpoints ──────────────────────────────────────────────────────

def test_cron_get_known_task(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.get("/admin/cron/run_weekly_scrappers")
    assert r.status_code == 200
    data = r.get_json()
    assert "expression" in data
    client.get("/admin/logout")


def test_cron_get_unknown_task(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.get("/admin/cron/nonexistent-task")
    assert r.status_code == 200
    data = r.get_json()
    assert data["expression"] == ""
    client.get("/admin/logout")


def test_cron_update_valid(client, admin_user, tmp_path, monkeypatch):
    """Route writes to <root_path>/config/cron_config.json — redirect to tmp_path."""
    from flask import current_app
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    monkeypatch.setattr(current_app._get_current_object(), "root_path", str(tmp_path))
    r = client.post(
        "/admin/cron/update",
        data={"task": "run_weekly_scrappers", "expression": "0 6 * * *"},
    )
    assert r.status_code == 200
    data = r.get_json()
    assert data["success"] is True
    client.get("/admin/logout")


def test_cron_update_missing_params(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.post("/admin/cron/update", data={"task": "", "expression": ""})
    assert r.status_code == 200
    data = r.get_json()
    assert data["success"] is False
    client.get("/admin/logout")


def test_cron_update_invalid_expression(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.post(
        "/admin/cron/update",
        data={"task": "run_weekly_scrappers", "expression": "not-a-cron"},
    )
    assert r.status_code == 200
    data = r.get_json()
    assert data["success"] is False
    client.get("/admin/logout")


def test_cron_run_unknown_task(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.post("/admin/cron/run/nonexistent-task")
    assert r.status_code == 200
    data = r.get_json()
    assert data["success"] is False
    client.get("/admin/logout")


# ── Admin ops endpoints ───────────────────────────────────────────────────────

def test_admin_ops_clear_logs(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.post("/admin/tools/clear-logs")
    assert r.status_code == 200
    data = r.get_json()
    assert "success" in data
    client.get("/admin/logout")


def test_admin_ops_export_data(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.get("/admin/tools/export-data")
    assert r.status_code == 200
    client.get("/admin/logout")


def test_admin_ops_delete_log_missing(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.delete("/admin/tools/delete-log/nonexistent.log")
    assert r.status_code == 200
    data = r.get_json()
    assert "success" in data
    client.get("/admin/logout")


def test_admin_ops_delete_report_missing(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.delete("/admin/tools/delete-report/nonexistent.json")
    assert r.status_code == 200
    data = r.get_json()
    assert "success" in data
    client.get("/admin/logout")


# ── Manage users ──────────────────────────────────────────────────────────────

def test_admin_manage_users_not_found(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.post("/admin/delete_user/99999", follow_redirects=False)
    assert r.status_code == 404
    client.get("/admin/logout")


# ── manage_users GET with pagination ─────────────────────────────────────────

def test_admin_manage_users_page1(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.get("/admin/manage_users?page=1")
    assert r.status_code == 200
    client.get("/admin/logout")
