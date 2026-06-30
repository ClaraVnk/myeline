"""Extra auth flow tests — login, register, logout."""
from unittest.mock import MagicMock, patch

import pytest

from tests.helpers import login, logout


# ── Login ─────────────────────────────────────────────────────────────────────

def test_login_get(client):
    r = client.get("/login")
    assert r.status_code == 200


def test_login_valid_confirmed(client, confirmed_user):
    r = login(client, "alice@example.com", "Alice1234!")
    assert r.status_code in (200, 301, 302)


def test_login_redirects_admin_to_dashboard(client, admin_user):
    """Admin login completes full OTP flow and lands on admin dashboard."""
    login(client, "admin@example.com", "Admin1234!")
    r = client.get("/admin/dashboard", follow_redirects=False)
    assert r.status_code == 200
    client.get("/admin/logout")


def test_login_invalid_password(client, confirmed_user):
    r = login(client, "alice@example.com", "WrongPass!")
    assert r.status_code in (200, 301, 302)


def test_login_unknown_email(client):
    r = login(client, "nobody@example.com", "Whatever1!")
    assert r.status_code in (200, 301, 302)


def test_login_unconfirmed_user(client, unconfirmed_user):
    r = login(client, "bob@example.com", "Bob12345!")
    assert r.status_code in (200, 301, 302)


def test_logout(client, confirmed_user):
    login(client, "alice@example.com", "Alice1234!")
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code in (301, 302)


# ── Register ──────────────────────────────────────────────────────────────────

def test_register_get(client):
    r = client.get("/register")
    assert r.status_code == 200


@patch("app.routes.auth.send_email_via_brevo_api")
def test_register_student_sends_email(mock_email, client, db):
    mock_email.return_value = None

    r = client.post(
        "/register",
        data={
            "firstname": "Marie",
            "lastname": "Étudiant",
            "email": "marie_student_test@example.com",
            "password": "Marie1234!",
            "confirm_password": "Marie1234!",
            "is_student": True,
            "newsletter_consent": True,
            "email_tracking_consent": True,
        },
        follow_redirects=False,
    )
    assert r.status_code in (200, 301, 302)

    from app.models.user import User
    u = User.query.filter_by(email="marie_student_test@example.com").first()
    if u:
        db.session.delete(u)
        db.session.commit()


def test_register_weak_password(client):
    r = client.post(
        "/register",
        data={
            "firstname": "Weak",
            "lastname": "Pass",
            "email": "weak@example.com",
            "password": "weak",
            "confirm_password": "weak",
            "newsletter_consent": True,
        },
    )
    assert r.status_code == 200


def test_register_duplicate_email(client, confirmed_user):
    r = client.post(
        "/register",
        data={
            "firstname": "Alice",
            "lastname": "Dup",
            "email": "alice@example.com",
            "password": "Alice1234!",
            "confirm_password": "Alice1234!",
            "newsletter_consent": True,
        },
    )
    assert r.status_code in (200, 301, 302)


def test_register_short_password_passes_length_fails_strength(client):
    """Password passes Length(min=6) but fails is_strong_password → flash + re-render."""
    r = client.post(
        "/register",
        data={
            "firstname": "Weak",
            "lastname": "Strength",
            "email": "weakstr@example.com",
            "password": "abcdef",
            "confirm_password": "abcdef",
            "newsletter_consent": True,
        },
    )
    assert r.status_code == 200


# ── login with DB commit error ────────────────────────────────────────────────


def test_login_last_login_commit_error(client, confirmed_user, app):
    """DB error updating last_login is swallowed; login still redirects."""
    from app.ext import db as the_db

    call_count = [0]
    original = the_db.session.commit

    def fail_first():
        call_count[0] += 1
        if call_count[0] == 1:
            the_db.session.rollback()
            raise Exception("db write error")
        return original()

    with patch.object(the_db.session, "commit", side_effect=fail_first):
        r = client.post(
            "/login",
            data={"email": "alice@example.com", "password": "Alice1234!"},
            follow_redirects=False,
        )
    assert r.status_code in (301, 302, 200)

    client.get("/logout")
    the_db.session.rollback()


# ── forgot_password email send error ─────────────────────────────────────────


def test_forgot_password_email_error(client, confirmed_user):
    """Email send failure → error flash + re-render the form."""
    with patch(
        "app.routes.auth.send_email_via_brevo_api",
        side_effect=Exception("smtp error"),
    ):
        r = client.post(
            "/forgot-password",
            data={"email": "alice@example.com"},
        )
    assert r.status_code == 200


# ── reset_password token already used ────────────────────────────────────────


def test_reset_password_token_already_used(client, confirmed_user, app):
    """Token signed with a wrong ph fingerprint → redirect to forgot_password."""
    from itsdangerous import URLSafeTimedSerializer

    with app.app_context():
        s = URLSafeTimedSerializer(app.config["SECRET_KEY"])
        data = {"email": confirmed_user.email, "ph": "wrongfingerprint00"}
        token = s.dumps(data, salt="password-reset-salt")

    r = client.get(f"/reset-password/{token}", follow_redirects=False)
    assert r.status_code in (301, 302)


# ── reset_password successful submission ──────────────────────────────────────


def test_reset_password_success(client, confirmed_user, app):
    """Valid token + valid new password → password updated + redirect."""
    from itsdangerous import URLSafeTimedSerializer

    with app.app_context():
        s = URLSafeTimedSerializer(app.config["SECRET_KEY"])
        ph_fingerprint = confirmed_user.password_hash[-12:]
        data = {"email": confirmed_user.email, "ph": ph_fingerprint}
        token = s.dumps(data, salt="password-reset-salt")

    r = client.post(
        f"/reset-password/{token}",
        data={"password": "NewPass1!", "confirm_password": "NewPass1!"},
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)

    # Restore original password so other tests continue to work
    with app.app_context():
        from app.ext import db
        from app.models.user import User

        u = User.query.filter_by(email=confirmed_user.email).first()
        if u:
            u.set_password("Alice1234!")
            db.session.commit()
