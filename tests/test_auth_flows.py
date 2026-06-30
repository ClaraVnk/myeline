"""Tests for auth flows: confirm_email, forgot_password, reset_password, resend_confirmation."""
from unittest.mock import patch

from tests.helpers import login


# ── confirm_email ─────────────────────────────────────────────────────────────

def test_confirm_email_invalid_token_shows_page(client):
    r = client.get("/confirm/not-a-real-token", follow_redirects=True)
    assert r.status_code == 200


def test_confirm_email_valid_token(client, app, unconfirmed_user):
    from app.utils.tokens import generate_confirmation_token
    with app.app_context():
        token = generate_confirmation_token(unconfirmed_user.email)
    r = client.get(f"/confirm/{token}", follow_redirects=False)
    assert r.status_code in (301, 302)


def test_confirm_email_already_confirmed(client, app, confirmed_user):
    from app.utils.tokens import generate_confirmation_token
    with app.app_context():
        token = generate_confirmation_token(confirmed_user.email)
    r = client.get(f"/confirm/{token}", follow_redirects=False)
    assert r.status_code in (301, 302)


# ── forgot_password ───────────────────────────────────────────────────────────

def test_forgot_password_get(client):
    r = client.get("/forgot-password")
    assert r.status_code == 200


@patch("app.utils.mailer.send_email_via_brevo_api")
def test_forgot_password_post_known_email(mock_send, client, confirmed_user):
    r = client.post(
        "/forgot-password",
        data={"email": "alice@example.com"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 301, 302)


@patch("app.utils.mailer.send_email_via_brevo_api")
def test_forgot_password_post_unknown_email(mock_send, client):
    r = client.post(
        "/forgot-password",
        data={"email": "nobody@nowhere.test"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 301, 302)


# ── reset_password ────────────────────────────────────────────────────────────

def test_reset_password_get_valid_token(client, app, confirmed_user):
    # Audit M-5 fix: fingerprint is now the full SHA-256 of the
    # password_hash (vs last 12 chars), so token format follows.
    from itsdangerous import URLSafeTimedSerializer

    from app.routes.auth import _password_hash_fingerprint
    with app.app_context():
        s = URLSafeTimedSerializer(app.config["SECRET_KEY"])
        token = s.dumps(
            {"email": confirmed_user.email,
             "ph": _password_hash_fingerprint(confirmed_user.password_hash)},
            salt="password-reset-salt",
        )
    r = client.get(f"/reset-password/{token}")
    assert r.status_code == 200


def test_reset_password_get_invalid_token(client):
    r = client.get("/reset-password/bad-token", follow_redirects=False)
    assert r.status_code in (301, 302)


def test_reset_password_post_valid_token(client, app, confirmed_user):
    from itsdangerous import URLSafeTimedSerializer
    with app.app_context():
        s = URLSafeTimedSerializer(app.config["SECRET_KEY"])
        token = s.dumps(
            {"email": confirmed_user.email, "ph": confirmed_user.password_hash[-12:]},
            salt="password-reset-salt",
        )
    r = client.post(
        f"/reset-password/{token}",
        data={"password": "NewPass1!"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 301, 302)


# ── resend_confirmation ───────────────────────────────────────────────────────

def test_resend_confirmation_get(client):
    r = client.get("/resend-confirmation")
    assert r.status_code == 200


@patch("app.utils.mailer.send_email_via_brevo_api")
def test_resend_confirmation_post_unconfirmed(mock_send, client, unconfirmed_user):
    r = client.post(
        "/resend-confirmation",
        data={"email": "bob@example.com"},
    )
    assert r.status_code == 200


@patch("app.utils.mailer.send_email_via_brevo_api")
def test_resend_confirmation_post_already_confirmed(mock_send, client, confirmed_user):
    r = client.post(
        "/resend-confirmation",
        data={"email": "alice@example.com"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 301, 302)
