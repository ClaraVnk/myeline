"""Debug cookie isolation between tests."""
from unittest.mock import patch


def test_admin_login_no_logout(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        client.post("/admin/login", data={"email": "admin@example.com", "password": "Admin1234!"})
        with client.session_transaction() as sess:
            code = sess.get("2fa_code")
        client.post("/admin/login", data={"email": "admin@example.com", "password": "Admin1234!", "two_factor_code": code})
    cookies_after = dict(client._cookies) if hasattr(client, '_cookies') else {}
    print(f"\nCLIENT ID: {id(client)}, COOKIES: {list(cookies_after.keys())}")


def test_next_client_no_cookies(client):
    cookies = dict(client._cookies) if hasattr(client, '_cookies') else {}
    print(f"\nNEXT CLIENT ID: {id(client)}, COOKIES: {list(cookies.keys())}")
    r = client.get("/admin/dashboard", follow_redirects=False)
    print(f"STATUS: {r.status_code}")
