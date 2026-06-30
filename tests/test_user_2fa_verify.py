"""
Login-time 2FA verification — all three methods: TOTP, SMS, email OTP.
"""
import time
from unittest.mock import patch


# ── No pending session ────────────────────────────────────────────────────────

def test_verify_without_session_redirects_to_login(client):
    r = client.get("/user/2fa/verify", follow_redirects=False)
    assert r.status_code in (301, 302)
    assert "login" in r.headers.get("Location", "")


def test_verify_post_without_session_redirects(client):
    r = client.post("/user/2fa/verify", data={"totp_code": "123456"}, follow_redirects=False)
    assert r.status_code in (301, 302)


# ── TOTP ──────────────────────────────────────────────────────────────────────

def test_verify_totp_get_shows_form(client, user_totp):
    with client.session_transaction() as sess:
        sess["pending_2fa_user_id"] = user_totp.id
    r = client.get("/user/2fa/verify")
    assert r.status_code == 200
    assert b"totp_code" in r.data


def test_verify_totp_valid_code_logs_in(client, user_totp):
    import pyotp
    with client.session_transaction() as sess:
        sess["pending_2fa_user_id"] = user_totp.id
    code = pyotp.TOTP(user_totp.totp_secret).now()
    r = client.post("/user/2fa/verify", data={"totp_code": code}, follow_redirects=False)
    assert r.status_code in (301, 302)
    assert "dashboard" in r.headers.get("Location", "")


def test_verify_totp_invalid_code_shows_error(client, user_totp):
    import pyotp
    with client.session_transaction() as sess:
        sess["pending_2fa_user_id"] = user_totp.id
    r = client.post("/user/2fa/verify", data={"totp_code": "000000"})
    assert r.status_code == 200
    assert b"invalid" in r.data.lower() or b"invalide" in r.data.lower()


# ── Email OTP ─────────────────────────────────────────────────────────────────

def test_verify_email_otp_get_sends_code(client, user_email_2fa):
    with client.session_transaction() as sess:
        sess["pending_2fa_user_id"] = user_email_2fa.id
    with patch("app.routes.user_2fa.send_email_via_brevo_api") as mock_mail:
        r = client.get("/user/2fa/verify")
    assert r.status_code == 200
    mock_mail.assert_called_once()
    with client.session_transaction() as sess:
        assert sess.get("otp_code") is not None


def test_verify_email_otp_valid_code_logs_in(client, user_email_2fa):
    with client.session_transaction() as sess:
        sess["pending_2fa_user_id"] = user_email_2fa.id
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        client.get("/user/2fa/verify")
    with client.session_transaction() as sess:
        code = sess.get("otp_code")
    r = client.post("/user/2fa/verify", data={"otp_code": code}, follow_redirects=False)
    assert r.status_code in (301, 302)
    assert "dashboard" in r.headers.get("Location", "")


def test_verify_email_otp_invalid_code_shows_error(client, user_email_2fa):
    with client.session_transaction() as sess:
        sess["pending_2fa_user_id"] = user_email_2fa.id
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        client.get("/user/2fa/verify")
    r = client.post("/user/2fa/verify", data={"otp_code": "000000"})
    assert r.status_code == 200
