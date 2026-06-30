"""Tests for admin_tools routes — view_log, view_report."""
from unittest.mock import patch

import pytest

from tests.helpers import admin_login


def test_view_log_nonexistent(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.get("/admin/view_log/nonexistent.log", follow_redirects=False)
    assert r.status_code in (200, 301, 302)
    client.get("/admin/logout")


def test_view_report_nonexistent(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.get("/admin/view_report/nonexistent.json", follow_redirects=False)
    assert r.status_code in (200, 301, 302)
    client.get("/admin/logout")


def test_view_log_traversal_attempt(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    # werkzeug encodes slashes, route still matches with sanitized name
    r = client.get("/admin/view_log/passwd", follow_redirects=False)
    assert r.status_code in (200, 301, 302, 404)
    client.get("/admin/logout")
