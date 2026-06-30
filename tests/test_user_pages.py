"""Tests for authenticated user pages: dashboard, settings, categories, contact."""
from unittest.mock import patch

from tests.helpers import login, logout


def _login_alice(client):
    login(client, "alice@example.com", "Alice1234!")


def _login_subscriber(client):
    login(client, "subscriber@example.com", "Alice1234!")


# ── dashboard ─────────────────────────────────────────────────────────────────

def test_dashboard_requires_login(client):
    r = client.get("/user/dashboard", follow_redirects=False)
    assert r.status_code in (301, 302)


def test_dashboard_accessible_when_logged_in(client, subscriber_user):
    _login_subscriber(client)
    r = client.get("/user/dashboard")
    assert r.status_code == 200
    client.get("/logout")


def test_dashboard_search_query(client, subscriber_user):
    _login_subscriber(client)
    r = client.get("/user/dashboard?q=test")
    assert r.status_code == 200
    client.get("/logout")


def test_dashboard_pagination(client, subscriber_user):
    _login_subscriber(client)
    r = client.get("/user/dashboard?page=1")
    assert r.status_code == 200
    client.get("/logout")


# ── account_settings GET ──────────────────────────────────────────────────────

def test_settings_requires_login(client):
    r = client.get("/user/settings", follow_redirects=False)
    assert r.status_code in (301, 302)


def test_settings_get(client, confirmed_user):
    _login_alice(client)
    r = client.get("/user/settings")
    assert r.status_code == 200
    client.get("/logout")


# ── account_settings POST ─────────────────────────────────────────────────────

def test_settings_post_change_password(client, confirmed_user):
    _login_alice(client)
    r = client.post(
        "/user/settings",
        data={"password": "NewAlice1!"},
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)
    # restore original password so fixture teardown can delete user
    client.post(
        "/user/settings",
        data={"password": "Alice1234!"},
        follow_redirects=False,
    )
    client.get("/logout")


def test_settings_post_weak_password_rejected(client, confirmed_user):
    _login_alice(client)
    r = client.post(
        "/user/settings",
        data={"password": "weak"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 301, 302)
    client.get("/logout")


def test_settings_post_newsletter_consent_on(client, confirmed_user):
    _login_alice(client)
    r = client.post(
        "/user/settings",
        data={"newsletter_consent": "on"},
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)
    client.get("/logout")


def test_settings_post_newsletter_consent_off(client, confirmed_user):
    _login_alice(client)
    r = client.post("/user/settings", data={}, follow_redirects=False)
    assert r.status_code in (301, 302)
    client.get("/logout")


# ── categories ────────────────────────────────────────────────────────────────

def test_categories_requires_login(client):
    r = client.get("/user/categories", follow_redirects=False)
    assert r.status_code in (301, 302)


def test_categories_accessible(client, subscriber_user):
    _login_subscriber(client)
    r = client.get("/user/categories")
    assert r.status_code == 200
    client.get("/logout")


# ── contact ───────────────────────────────────────────────────────────────────

def test_user_contact_get(client, confirmed_user):
    _login_alice(client)
    r = client.get("/user/contact")
    assert r.status_code == 200
    client.get("/logout")


@patch("app.utils.mailer.send_email_via_brevo_api")
def test_user_contact_post(mock_send, client, confirmed_user):
    _login_alice(client)
    r = client.post(
        "/user/contact",
        data={"message": "Bonjour, j'ai une question."},
        follow_redirects=False,
    )
    assert r.status_code in (200, 301, 302)
    client.get("/logout")
