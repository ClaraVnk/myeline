"""Strategic tests on auth login error paths.

Covers app/routes/auth.py login error branches (unknown email / wrong
password → generic flash). The SSO redirect endpoint and the Stripe
webhook handlers were removed in the OSS single-user build, so the
tests that exercised them are gone.
"""


# ── auth.py login form — error paths ─────────────────────────────────────────

class TestLoginErrorPaths:
    def test_login_unknown_email_flashes_invalid(self, client):
        """An unknown email returns the login page with `Identifiants invalides`."""
        r = client.post("/login", data={
            "email": "nobody@example.com",
            "password": "irrelevant",
        }, follow_redirects=True)
        # The flash banner text appears in the response body.
        assert b"Identifiants invalides" in r.data or b"invalides" in r.data

    def test_login_wrong_password_flashes_invalid(self, client, db, app):
        """Existing email with wrong password → same generic flash."""
        from app.models.user import User
        with app.app_context():
            u = User(email="login-test@example.com",
                     is_confirmed=True, is_active=True)
            u.set_password("RealPass123!")
            db.session.add(u)
            db.session.commit()
        r = client.post("/login", data={
            "email": "login-test@example.com",
            "password": "WrongPass",
        }, follow_redirects=True)
        assert b"invalides" in r.data.lower() or b"identifiants" in r.data.lower()
