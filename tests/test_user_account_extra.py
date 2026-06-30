"""Tests for user account settings — password change, consents, contact."""
from unittest.mock import patch

from tests.helpers import login, logout


def test_account_settings_get(client, confirmed_user):
    login(client, "alice@example.com", "Alice1234!")
    r = client.get("/user/settings")
    assert r.status_code == 200
    logout(client)


def test_account_settings_post_password_weak(client, confirmed_user):
    login(client, "alice@example.com", "Alice1234!")
    r = client.post(
        "/user/settings",
        data={"password": "weak"},
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)
    logout(client)


def test_account_settings_post_password_strong(client, confirmed_user, db):
    login(client, "alice@example.com", "Alice1234!")
    r = client.post(
        "/user/settings",
        data={"password": "NewStrong1!"},
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)
    # restore password so other tests aren't affected
    confirmed_user.set_password("Alice1234!")
    db.session.commit()
    logout(client)


def test_account_settings_post_newsletter_consent_on(client, confirmed_user, db):
    login(client, "alice@example.com", "Alice1234!")
    r = client.post(
        "/user/settings",
        data={"newsletter_consent": "on"},
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)
    logout(client)


def test_account_settings_post_newsletter_consent_off(client, confirmed_user, db):
    login(client, "alice@example.com", "Alice1234!")
    client.post("/user/settings", data={"newsletter_consent": "on"}, follow_redirects=False)
    r = client.post("/user/settings", data={}, follow_redirects=False)
    assert r.status_code in (301, 302)
    logout(client)


def test_account_settings_post_email_tracking_on(client, confirmed_user):
    login(client, "alice@example.com", "Alice1234!")
    r = client.post(
        "/user/settings",
        data={"email_tracking_consent": "on"},
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)
    logout(client)


def test_contact_get(client, confirmed_user):
    login(client, "alice@example.com", "Alice1234!")
    r = client.get("/user/contact")
    assert r.status_code == 200
    logout(client)


def test_contact_post(client, confirmed_user):
    login(client, "alice@example.com", "Alice1234!")
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        r = client.post(
            "/user/contact",
            data={"email": "alice@example.com", "message": "Test message here"},
            follow_redirects=False,
        )
    assert r.status_code in (200, 301, 302)
    logout(client)


def test_categories_page(client, subscriber_user):
    login(client, "subscriber@example.com", "Alice1234!")
    r = client.get("/user/categories")
    assert r.status_code == 200
    logout(client)
