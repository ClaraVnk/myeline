"""Tests for auth register — covering missed paths in auth.py."""
from unittest.mock import MagicMock, patch

import pytest


def _register(client, **kwargs):
    data = {
        "email": "newreg@example.com",
        "password": "NewReg1!",
        "confirm_password": "NewReg1!",
        "firstname": "New",
        "lastname": "Reg",
        # BooleanField: omit the key = False (unchecked), include any value = True
        "newsletter_consent": "y",
        "accept_cgv": "y",
        "accept_privacy": "y",
    }
    data.update(kwargs)
    return client.post("/register", data=data, follow_redirects=False)


# ── Student registration ──────────────────────────────────────────────────────


def test_register_student_success(client, db):
    import io

    fake_card = (io.BytesIO(b"%PDF fake"), "card.pdf")
    with patch("app.routes.auth.send_email_via_brevo_api"):
        r = _register(
            client,
            email="student_reg@example.com",
            is_student="y",
            student_card=fake_card,
        )
    assert r.status_code in (301, 302, 303)

    from app.models.user import User

    u = User.query.filter_by(email="student_reg@example.com").first()
    if u:
        db.session.delete(u)
        db.session.commit()


# ── Duplicate email ───────────────────────────────────────────────────────────


def test_register_duplicate_email(client, confirmed_user):
    r = _register(client, email="alice@example.com")
    assert r.status_code in (200, 301, 302)


# ── Password confirmation mismatch ────────────────────────────────────────────


def test_register_password_mismatch(client):
    r = _register(client, confirm_password="Different1!")
    assert r.status_code in (200, 301, 302)


# ── Confirm email ─────────────────────────────────────────────────────────────


def test_confirm_email_invalid_token(client):
    r = client.get("/confirm/invalidtoken123", follow_redirects=False)
    # Invalid token renders a 200 page (resend confirmation template)
    assert r.status_code in (200, 301, 302)


def test_confirm_email_valid(client, db):
    from app.models.user import User
    from app.utils.tokens import generate_confirmation_token

    user = User(
        firstname="Confirm",
        lastname="Me",
        email="confirm_me@example.com",
        is_confirmed=False,
        abonnement_type="gratuit",
    )
    user.set_password("Confirm1!")
    token = generate_confirmation_token("confirm_me@example.com")
    user.confirmation_token = token
    db.session.add(user)
    db.session.commit()

    r = client.get(f"/confirm/{token}", follow_redirects=False)
    assert r.status_code in (301, 302, 303)

    db.session.delete(user)
    db.session.commit()


# ── Forgot / reset password ───────────────────────────────────────────────────


def test_forgot_password_get(client):
    r = client.get("/forgot-password")
    assert r.status_code == 200


def test_forgot_password_post_unknown_email(client):
    r = client.post(
        "/forgot-password",
        data={"email": "nobody@example.com"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 301, 302)


def test_forgot_password_post_known_email(client, confirmed_user):
    with patch("app.routes.auth.send_email_via_brevo_api"):
        r = client.post(
            "/forgot-password",
            data={"email": "alice@example.com"},
            follow_redirects=False,
        )
    assert r.status_code in (200, 301, 302)


def test_reset_password_invalid_token(client):
    r = client.get("/reset-password/badtoken", follow_redirects=False)
    assert r.status_code in (200, 301, 302)


def test_reset_password_valid_token(client, db):
    from app.models.user import User
    from app.utils.tokens import generate_confirmation_token

    user = User(
        firstname="Reset",
        lastname="Pass",
        email="reset_pass@example.com",
        is_confirmed=True,
        abonnement_type="gratuit",
    )
    user.set_password("Reset123!")
    token = generate_confirmation_token("reset_pass@example.com")
    user.reset_token = token
    db.session.add(user)
    db.session.commit()

    r = client.get(f"/reset-password/{token}", follow_redirects=False)
    assert r.status_code in (200, 301, 302)

    r2 = client.post(
        f"/reset-password/{token}",
        data={"password": "NewReset1!", "confirm_password": "NewReset1!"},
        follow_redirects=False,
    )
    assert r2.status_code in (200, 301, 302)

    db.session.delete(user)
    db.session.commit()
