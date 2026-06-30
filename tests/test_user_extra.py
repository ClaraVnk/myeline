"""Extra tests for user routes — dashboard search, settings POST, contact POST."""
from unittest.mock import MagicMock, patch

import pytest

from tests.helpers import login


def _login_alice(client):
    login(client, "alice@example.com", "Alice1234!")


def _login_subscriber(client):
    login(client, "subscriber@example.com", "Alice1234!")


# ── Dashboard ─────────────────────────────────────────────────────────────────

def test_dashboard_search_query(client, subscriber_user):
    _login_subscriber(client)
    r = client.get("/user/dashboard?q=cardio")
    assert r.status_code == 200
    client.get("/logout")


def test_dashboard_page1(client, subscriber_user):
    _login_subscriber(client)
    r = client.get("/user/dashboard?page=1")
    assert r.status_code == 200
    client.get("/logout")


# ── Settings POST ─────────────────────────────────────────────────────────────

def test_settings_post_password_change(client, confirmed_user):
    _login_alice(client)
    r = client.post(
        "/user/settings",
        data={"password": "NewAlice1234!"},
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)
    client.get("/logout")
    # restore original password so other tests aren't broken
    from app.models.user import User
    u = User.query.filter_by(email="alice@example.com").first()
    if u:
        u.set_password("Alice1234!")
        from app.ext import db
        db.session.commit()


def test_settings_post_weak_password(client, confirmed_user):
    _login_alice(client)
    r = client.post(
        "/user/settings",
        data={"password": "weak"},
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)
    client.get("/logout")


def test_settings_post_newsletter_consent(client, confirmed_user):
    _login_alice(client)
    r = client.post(
        "/user/settings",
        data={"newsletter_consent": "on"},
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)
    client.get("/logout")


def test_settings_post_tracking_consent(client, confirmed_user):
    _login_alice(client)
    r = client.post(
        "/user/settings",
        data={"email_tracking_consent": "on"},
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)
    client.get("/logout")


# ── Contact POST ──────────────────────────────────────────────────────────────

@patch("app.routes.user.send_email_via_brevo_api")
def test_user_contact_post_success(mock_send, client, confirmed_user):
    mock_send.return_value = None
    _login_alice(client)
    r = client.post(
        "/user/contact",
        data={"message": "Hello, I have a question."},
        follow_redirects=False,
    )
    assert r.status_code in (200, 301, 302)
    client.get("/logout")


def test_user_contact_post_empty(client, confirmed_user):
    _login_alice(client)
    r = client.post("/user/contact", data={"message": ""})
    assert r.status_code == 200
    client.get("/logout")


# ── Categories ────────────────────────────────────────────────────────────────

def test_user_categories(client, subscriber_user):
    _login_subscriber(client)
    r = client.get("/user/categories")
    assert r.status_code == 200
    client.get("/logout")


# ── Contact form email exception ──────────────────────────────────────────────

def test_contact_form_email_exception(client, confirmed_user):
    """Exception when sending contact email → flash danger, redirect."""
    _login_alice(client)
    with patch(
        "app.routes.user.send_email_via_brevo_api",
        side_effect=Exception("smtp down"),
    ):
        r = client.post(
            "/user/contact",
            data={"email": "alice@example.com", "message": "Hello from test, more than 10 chars"},
            follow_redirects=False,
        )
    assert r.status_code in (200, 301, 302)
    client.get("/logout")


# ── account_settings commit error ─────────────────────────────────────────────

def test_account_settings_commit_error(client, confirmed_user):
    """DB commit error on settings save → flash danger, redirect."""
    _login_alice(client)
    from app.ext import db as the_db

    original = the_db.session.commit
    call_count = [0]

    def fail_once():
        call_count[0] += 1
        if call_count[0] == 1:
            the_db.session.rollback()
            raise Exception("db error")
        return original()

    with patch.object(the_db.session, "commit", side_effect=fail_once):
        r = client.post(
            "/user/settings",
            data={"newsletter_consent": "on"},
            follow_redirects=False,
        )
    assert r.status_code in (301, 302)
    the_db.session.rollback()
    client.get("/logout")


# ── delete_account commit error ────────────────────────────────────────────────

def test_delete_account_commit_error(client, db):
    """DB commit error during delete_account → flash danger, redirect to settings."""
    from app.models.user import User

    user = User(
        firstname="DelCommit",
        lastname="Err",
        email="del_commit_err@example.com",
        is_confirmed=True,
        abonnement_type="gratuit",
    )
    user.set_password("DelCommit1!")
    db.session.add(user)
    db.session.commit()

    login(client, "del_commit_err@example.com", "DelCommit1!")
    from app.ext import db as the_db

    original = the_db.session.commit
    call_count = [0]

    def fail_once():
        call_count[0] += 1
        if call_count[0] == 1:
            the_db.session.rollback()
            raise Exception("commit failed")
        return original()

    with patch.object(the_db.session, "commit", side_effect=fail_once), \
         patch("app.routes.user_tools.send_email_via_brevo_api"):
        r = client.post("/user/delete_account", data={}, follow_redirects=False)
    assert r.status_code in (301, 302)

    the_db.session.rollback()
    u = User.query.filter_by(email="del_commit_err@example.com").first()
    if u:
        db.session.delete(u)
        db.session.commit()


# ── delete_account outer exception ────────────────────────────────────────────

def test_delete_account_outer_exception(client, db):
    """Unexpected exception during delete_account → flash danger, redirect."""
    from app.models.user import User

    user = User(
        firstname="DelExc",
        lastname="Outer",
        email="del_outer_exc@example.com",
        is_confirmed=True,
        abonnement_type="gratuit",
    )
    user.set_password("DelExc1!")
    db.session.add(user)
    db.session.commit()

    login(client, "del_outer_exc@example.com", "DelExc1!")
    with patch(
        "app.routes.user_tools.logout_user",
        side_effect=Exception("unexpected"),
    ):
        r = client.post("/user/delete_account", data={}, follow_redirects=False)
    assert r.status_code in (301, 302)

    u = User.query.filter_by(email="del_outer_exc@example.com").first()
    if u:
        db.session.delete(u)
        db.session.commit()
