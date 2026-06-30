"""Helpers partagés pour les tests fonctionnels."""
from unittest.mock import patch


def login(client, email, password):
    """Login a user, completing the 2FA flow automatically.

    - Admin users: credentials → admin OTP (email) → /admin/otp verify
    - Regular users with email/SMS 2FA: credentials → GET /2fa/verify (code sent) → POST verify
    - Regular users without 2FA: temporarily logged in, redirected to setup
    """
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        with patch("app.routes.user_2fa.send_email_via_brevo_api"):
            r = client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
            location = r.headers.get("Location", "")

            if "admin/login" in location or "admin/otp" in location:
                client.get("/admin/login")
                with client.session_transaction() as sess:
                    code = sess.get("admin_2fa_code")
                if code:
                    client.post("/admin/otp", data={"otp_code": code})
            elif "verify" in location:
                client.get("/user/2fa/verify")
                with client.session_transaction() as sess:
                    code = sess.get("otp_code")
                if code:
                    client.post("/user/2fa/verify", data={"otp_code": code})
    return r


def logout(client):
    return client.get("/logout", follow_redirects=False)


def admin_login(client, email="admin@example.com", password="Admin1234!"):
    """Complete the admin 2FA login flow (credentials → OTP page → verify)."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        client.post("/admin/login", data={"email": email, "password": password})
        with client.session_transaction() as sess:
            code = sess.get("admin_2fa_code")
        client.post("/admin/otp", data={"otp_code": code})


def admin_logout(client):
    return client.get("/admin/logout", follow_redirects=False)
