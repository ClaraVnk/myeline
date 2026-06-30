"""Admin authentication — two-step flow: credentials then OTP."""
from unittest.mock import patch


def test_admin_login_get(client, admin_user):
    r = client.get("/admin/login")
    assert r.status_code == 200


def test_admin_login_wrong_credentials(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        r = client.post(
            "/admin/login",
            data={"email": "admin@example.com", "password": "wrongpassword"},
            follow_redirects=False,
        )
    assert r.status_code in (200, 301, 302)
    location = r.headers.get("Location", "")
    assert "dashboard" not in location


def test_admin_login_valid_credentials_redirects_to_otp(client, admin_user):
    """Valid credentials → OTP sent, redirect to /admin/otp."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        r = client.post(
            "/admin/login",
            data={"email": "admin@example.com", "password": "Admin1234!"},
            follow_redirects=False,
        )
    assert r.status_code in (301, 302)
    assert "otp" in r.headers.get("Location", "")


def test_admin_otp_get_without_session_redirects(client):
    """Accessing /admin/otp without a pending OTP session → redirect to login."""
    r = client.get("/admin/otp", follow_redirects=False)
    assert r.status_code in (301, 302)
    assert "login" in r.headers.get("Location", "")


def test_admin_otp_wrong_code_does_not_authenticate(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        client.post("/admin/login", data={"email": "admin@example.com", "password": "Admin1234!"})
        r = client.post("/admin/otp", data={"otp_code": "000000"}, follow_redirects=False)
    assert r.status_code in (200, 301, 302)
    location = r.headers.get("Location", "")
    assert "dashboard" not in location


def test_admin_otp_correct_code_authenticates(client, admin_user):
    """Correct OTP → authenticated and redirected to dashboard."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        client.post("/admin/login", data={"email": "admin@example.com", "password": "Admin1234!"})
        with client.session_transaction() as sess:
            code = sess.get("admin_2fa_code")
        r = client.post("/admin/otp", data={"otp_code": code}, follow_redirects=False)
    assert r.status_code in (301, 302)
    assert "dashboard" in r.headers.get("Location", "")


def test_admin_otp_code_popped_from_session_after_success(client, admin_user):
    """Audit round 4 — A4-M1. The OTP code must be removed from the
    session as soon as it has been verified — even on the failure path —
    so a concurrent request or a session leak can't replay the same code."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        client.post("/admin/login", data={"email": "admin@example.com", "password": "Admin1234!"})
        with client.session_transaction() as sess:
            code = sess.get("admin_2fa_code")
        r = client.post("/admin/otp", data={"otp_code": code}, follow_redirects=False)
        with client.session_transaction() as sess:
            assert sess.get("admin_2fa_code") is None
    assert r.status_code in (301, 302)


def test_admin_otp_code_popped_even_on_wrong_submit(client, admin_user):
    """Failed verifications also consume the code so an attacker can't
    brute-force the 6 digits across multiple POSTs."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        client.post("/admin/login", data={"email": "admin@example.com", "password": "Admin1234!"})
        with client.session_transaction() as sess:
            assert sess.get("admin_2fa_code")  # code was issued
        client.post("/admin/otp", data={"otp_code": "000000"}, follow_redirects=False)
        with client.session_transaction() as sess:
            # Code consumed by the wrong submit — next try must fail with
            # "session expired" and force a fresh login.
            assert sess.get("admin_2fa_code") is None


def test_admin_already_authenticated_redirected_from_login(client, admin_user):
    """Admin already logged in → /admin/login redirects to dashboard."""
    from tests.helpers import admin_login
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.get("/admin/login", follow_redirects=False)
    assert r.status_code in (301, 302)
    assert "dashboard" in r.headers.get("Location", "")
