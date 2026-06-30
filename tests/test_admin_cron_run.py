"""Tests for admin cron routes — get/update/run cron tasks."""
from unittest.mock import patch

import pytest

from tests.helpers import admin_login


def test_get_cron_config_known_task(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.get("/admin/cron/cleanup_rgpd")
    assert r.status_code == 200
    data = r.get_json()
    assert "expression" in data
    client.get("/admin/logout")


def test_get_cron_config_unknown_task(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.get("/admin/cron/nonexistent_task")
    assert r.status_code == 200
    data = r.get_json()
    assert data["expression"] == ""
    client.get("/admin/logout")


def test_update_cron_config_valid(client, admin_user, tmp_path, monkeypatch):
    """The route writes to <app.root_path>/config/cron_config.json — redirect
    that path to a tmp_path so the test doesn't depend on app/config being
    writable from inside the container."""
    from flask import current_app
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    monkeypatch.setattr(current_app._get_current_object(), "root_path", str(tmp_path))

    r = client.post("/admin/cron/update", data={
        "task": "cleanup_rgpd",
        "expression": "0 3 * * *",
    })
    assert r.status_code == 200
    data = r.get_json()
    assert data["success"] is True
    client.get("/admin/logout")


def test_update_cron_config_invalid_expression(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.post("/admin/cron/update", data={
        "task": "cleanup_rgpd",
        "expression": "not-a-cron",
    })
    assert r.status_code == 200
    data = r.get_json()
    assert data["success"] is False
    client.get("/admin/logout")


def test_update_cron_config_missing_params(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.post("/admin/cron/update", data={})
    assert r.status_code == 200
    data = r.get_json()
    assert data["success"] is False
    client.get("/admin/logout")


def test_run_cron_task_unknown(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.post("/admin/cron/run/nonexistent_task")
    assert r.status_code == 200
    data = r.get_json()
    assert data["success"] is False
    client.get("/admin/logout")


def test_run_cron_task_known(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    with patch("threading.Thread") as mock_thread:
        mock_thread.return_value.start.return_value = None
        r = client.post("/admin/cron/run/cleanup_rgpd")
    assert r.status_code == 200
    data = r.get_json()
    assert data["success"] is True
    client.get("/admin/logout")
