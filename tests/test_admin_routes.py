"""
Tests for admin routes — dashboard, manage_users, stats, tags, review_queue, tools.
All tests use the full 2FA admin login flow via helpers.admin_login().
"""
from unittest.mock import patch

from tests.helpers import admin_login, admin_logout


# ── Auth guard ────────────────────────────────────────────────────────────────

def test_admin_dashboard_requires_auth(client):
    r = client.get("/admin/dashboard", follow_redirects=False)
    assert r.status_code in (301, 302)


def test_admin_login_get(client):
    r = client.get("/admin/login")
    assert r.status_code == 200


def test_admin_login_wrong_credentials(client, admin_user):
    r = client.post(
        "/admin/login",
        data={"email": "admin@example.com", "password": "wrong"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 301, 302)


# ── Dashboard ─────────────────────────────────────────────────────────────────

def test_admin_dashboard(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.get("/admin/dashboard")
    assert r.status_code == 200
    client.get("/admin/logout")


def test_admin_dashboard_pagination(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.get("/admin/dashboard?page=1")
    assert r.status_code == 200
    client.get("/admin/logout")


# ── Manage users ──────────────────────────────────────────────────────────────

def test_admin_manage_users_get(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.get("/admin/manage_users")
    assert r.status_code == 200
    client.get("/admin/logout")


def test_admin_manage_users_search(client, admin_user, confirmed_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.get("/admin/manage_users?search=alice")
    assert r.status_code == 200
    client.get("/admin/logout")


def test_admin_manage_users_pending_cards(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.get("/admin/manage_users?pending_cards_only=true")
    assert r.status_code == 200
    client.get("/admin/logout")


@patch("app.routes.admin.manage_users.send_email_via_brevo_api")
def test_admin_create_user(mock_send, client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.post(
        "/admin/create_user",
        data={
            "email": "newuser_test@example.com",
            "first_name": "Test",
            "last_name": "User",
        },
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)
    client.get("/admin/logout")


@patch("app.utils.mailer.send_email_via_brevo_api")
def test_admin_create_user_duplicate(mock_send, client, admin_user, confirmed_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.post(
        "/admin/create_user",
        data={"email": "alice@example.com", "first_name": "Alice", "last_name": "D"},
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)
    client.get("/admin/logout")


@patch("app.utils.mailer.send_email_via_brevo_api")
def test_admin_create_user_missing_email(mock_send, client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.post(
        "/admin/create_user",
        data={"email": "", "first_name": "X", "last_name": "Y"},
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)
    client.get("/admin/logout")


def test_admin_delete_user(client, app, admin_user, db):
    from app.models.user import User

    user = User(
        firstname="Del",
        lastname="Me",
        email="del_admin_test@example.com",
        is_admin=False,
        is_confirmed=True,
        abonnement_type="gratuit",
    )
    user.set_password("Del12345!")
    db.session.add(user)
    db.session.commit()
    uid = user.id

    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.post(f"/admin/delete_user/{uid}", follow_redirects=False)
    assert r.status_code in (301, 302)
    with app.app_context():
        from app.ext import db as _db
        from app.models.user import User as _User
        assert _db.session.get(_User, uid) is None
    client.get("/admin/logout")


# ── Cron status ───────────────────────────────────────────────────────────────

def test_admin_cron_status_get(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.get("/admin/cron/run_weekly_scrappers")
    assert r.status_code == 200
    client.get("/admin/logout")


# ── Admin logout ──────────────────────────────────────────────────────────────

def test_admin_logout(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.get("/admin/logout", follow_redirects=False)
    assert r.status_code in (301, 302)
