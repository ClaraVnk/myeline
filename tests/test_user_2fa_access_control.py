"""
Mandatory 2FA enforcement via before_request hook.
Users without 2FA configured cannot access any protected route.
Admin users bypass the check entirely.
"""
from unittest.mock import patch


def _force_login(client, user):
    """Directly set Flask-Login session for a user (bypasses 2FA flow)."""
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


# ── User without 2FA ──────────────────────────────────────────────────────────

def test_user_without_2fa_redirected_from_dashboard(client, subscriber_no_2fa):
    """A logged-in user with no 2FA is redirected to /2fa/setup on any protected route."""
    _force_login(client, subscriber_no_2fa)
    r = client.get("/user/dashboard", follow_redirects=False)
    assert r.status_code in (301, 302)
    assert "2fa" in r.headers.get("Location", "").lower() or "setup" in r.headers.get("Location", "")


def test_user_without_2fa_can_access_setup_page(client, subscriber_no_2fa):
    """User without 2FA can reach /2fa/setup (it's in the allow-list)."""
    _force_login(client, subscriber_no_2fa)
    r = client.get("/user/2fa/setup")
    assert r.status_code == 200


def test_user_without_2fa_can_logout(client, subscriber_no_2fa):
    """User without 2FA can still log out."""
    _force_login(client, subscriber_no_2fa)
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code in (301, 302)


def test_user_without_2fa_cannot_access_settings(client, subscriber_no_2fa):
    """User without 2FA is blocked from account settings."""
    _force_login(client, subscriber_no_2fa)
    r = client.get("/user/settings", follow_redirects=False)
    assert r.status_code in (301, 302)
    assert "2fa" in r.headers.get("Location", "").lower() or "setup" in r.headers.get("Location", "")


# ── User with 2FA ─────────────────────────────────────────────────────────────

def test_user_with_totp_not_blocked_by_2fa_check(client, user_totp):
    """A subscriber with TOTP is not redirected to setup (may still be redirected for other reasons)."""
    _force_login(client, user_totp)
    r = client.get("/user/dashboard", follow_redirects=False)
    # Should not redirect to 2fa setup
    location = r.headers.get("Location", "")
    assert "setup" not in location or "2fa" not in location


def test_user_with_email_2fa_not_blocked(client, user_email_2fa):
    _force_login(client, user_email_2fa)
    r = client.get("/user/dashboard", follow_redirects=False)
    location = r.headers.get("Location", "")
    assert "2fa/setup" not in location


# ── Admin bypass ──────────────────────────────────────────────────────────────

def test_admin_without_2fa_not_blocked(client, admin_user):
    """Admin users bypass the mandatory 2FA before_request check."""
    _force_login(client, admin_user)
    r = client.get("/admin/dashboard")
    assert r.status_code == 200


# ── Login flow for users without 2FA ─────────────────────────────────────────

def test_login_without_2fa_redirects_to_setup(client, subscriber_no_2fa):
    """POST /login for user without 2FA → redirected to setup_2fa (and login_user called)."""
    r = client.post(
        "/login",
        data={"email": "no2fa@example.com", "password": "No2fa123!"},
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)
    assert "2fa" in r.headers.get("Location", "").lower()


def test_login_with_2fa_redirects_to_verify(client, user_totp):
    """POST /login for user with TOTP → redirected to 2fa/verify."""
    r = client.post(
        "/login",
        data={"email": "totp@example.com", "password": "Totp1234!"},
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)
    assert "verify" in r.headers.get("Location", "").lower()
