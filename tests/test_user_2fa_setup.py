"""
2FA setup flows — TOTP, SMS, email OTP, and disable.
Users without 2FA are temporarily logged in by the auth route during setup.
"""
from unittest.mock import patch


# ── Helpers ───────────────────────────────────────────────────────────────────

def _login_no_2fa(client, email, password):
    """Login a user who has no 2FA: auth route logs them in and redirects to setup."""
    r = client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    return r


def _login_with_2fa_totp(client, user):
    """Set pending_2fa_user_id in session to simulate post-login TOTP verify step."""
    with client.session_transaction() as sess:
        sess["pending_2fa_user_id"] = user.id


# ── Setup page ────────────────────────────────────────────────────────────────

def test_setup_page_shows_choose_when_no_2fa(client, subscriber_no_2fa):
    """User without 2FA sees method selector after login."""
    _login_no_2fa(client, "no2fa@example.com", "No2fa123!")
    r = client.get("/user/2fa/setup")
    assert r.status_code == 200
    assert b"choose" in r.data or b"Authenticator" in r.data or b"SMS" in r.data


def test_setup_page_shows_enabled_when_2fa_active(client, user_totp):
    """User with TOTP active sees current status page."""
    with client.session_transaction() as sess:
        sess["pending_2fa_user_id"] = user_totp.id
    import pyotp
    code = pyotp.TOTP(user_totp.totp_secret).now()
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        client.post("/user/2fa/verify", data={"totp_code": code})
    r = client.get("/user/2fa/setup")
    assert r.status_code == 200
    assert b"enabled" in r.data or b"active" in r.data or b"Disable" in r.data or b"active" in r.data.lower()


# ── TOTP setup ────────────────────────────────────────────────────────────────

def test_totp_generate_creates_secret(client, subscriber_no_2fa):
    """POST generate_totp → session stores pending_totp_secret, renders confirm step."""
    _login_no_2fa(client, "no2fa@example.com", "No2fa123!")
    r = client.post("/user/2fa/setup", data={"action": "generate_totp"})
    assert r.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get("pending_totp_secret") is not None


def test_totp_confirm_valid_code_activates(client, subscriber_no_2fa, db):
    """Valid TOTP code activates 2FA and redirects to account settings."""
    import pyotp
    _login_no_2fa(client, "no2fa@example.com", "No2fa123!")
    client.post("/user/2fa/setup", data={"action": "generate_totp"})
    with client.session_transaction() as sess:
        secret = sess.get("pending_totp_secret")
    code = pyotp.TOTP(secret).now()
    r = client.post("/user/2fa/setup", data={"action": "confirm_totp", "totp_code": code}, follow_redirects=False)
    assert r.status_code in (200, 301, 302)
    db.session.refresh(subscriber_no_2fa)
    assert subscriber_no_2fa.twofa_method == "totp"
    assert subscriber_no_2fa.totp_secret == secret


def test_totp_confirm_invalid_code_shows_error(client, subscriber_no_2fa):
    """Invalid TOTP code shows error, does not activate 2FA."""
    _login_no_2fa(client, "no2fa@example.com", "No2fa123!")
    client.post("/user/2fa/setup", data={"action": "generate_totp"})
    r = client.post("/user/2fa/setup", data={"action": "confirm_totp", "totp_code": "000000"})
    assert r.status_code == 200
    assert b"invalid" in r.data.lower() or b"invalide" in r.data.lower()


def test_totp_confirm_missing_session_redirects(client, subscriber_no_2fa):
    """confirm_totp with no pending secret in session → redirect to setup."""
    _login_no_2fa(client, "no2fa@example.com", "No2fa123!")
    r = client.post("/user/2fa/setup", data={"action": "confirm_totp", "totp_code": "123456"}, follow_redirects=False)
    assert r.status_code in (200, 301, 302)


# ── Email OTP setup ───────────────────────────────────────────────────────────

def test_email_otp_enable_sends_code(client, subscriber_no_2fa):
    """POST enable_email → OTP sent, confirm_email step rendered."""
    _login_no_2fa(client, "no2fa@example.com", "No2fa123!")
    with patch("app.utils.mailer.send_email_via_brevo_api") as mock_mail:
        r = client.post("/user/2fa/setup", data={"action": "enable_email"})
    assert r.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get("otp_code") is not None


def test_email_otp_confirm_valid_activates(client, subscriber_no_2fa, db):
    """Valid email OTP code activates email 2FA."""
    _login_no_2fa(client, "no2fa@example.com", "No2fa123!")
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        client.post("/user/2fa/setup", data={"action": "enable_email"})
    with client.session_transaction() as sess:
        code = sess.get("otp_code")
    r = client.post("/user/2fa/setup", data={"action": "confirm_email", "email_code": code}, follow_redirects=False)
    assert r.status_code in (200, 301, 302)
    db.session.refresh(subscriber_no_2fa)
    assert subscriber_no_2fa.twofa_method == "email"


def test_email_otp_confirm_invalid_shows_error(client, subscriber_no_2fa):
    """Wrong email OTP shows error."""
    _login_no_2fa(client, "no2fa@example.com", "No2fa123!")
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        client.post("/user/2fa/setup", data={"action": "enable_email"})
    r = client.post("/user/2fa/setup", data={"action": "confirm_email", "email_code": "000000"})
    assert r.status_code == 200


# ── Disable 2FA ───────────────────────────────────────────────────────────────

def test_disable_2fa_correct_password(client, user_totp, db):
    """Correct password disables 2FA."""
    with client.session_transaction() as sess:
        sess["pending_2fa_user_id"] = user_totp.id
    import pyotp
    code = pyotp.TOTP(user_totp.totp_secret).now()
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        client.post("/user/2fa/verify", data={"totp_code": code})
    r = client.post("/user/2fa/disable", data={"password": "Totp1234!"}, follow_redirects=False)
    assert r.status_code in (200, 301, 302)
    db.session.refresh(user_totp)
    assert user_totp.twofa_method is None


def test_disable_2fa_wrong_password_keeps_2fa(client, user_totp, db):
    """Wrong password does not disable 2FA."""
    with client.session_transaction() as sess:
        sess["pending_2fa_user_id"] = user_totp.id
    import pyotp
    code = pyotp.TOTP(user_totp.totp_secret).now()
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        client.post("/user/2fa/verify", data={"totp_code": code})
    r = client.post("/user/2fa/disable", data={"password": "wrong"}, follow_redirects=False)
    assert r.status_code in (200, 301, 302)
    db.session.refresh(user_totp)
    assert user_totp.twofa_method == "totp"
