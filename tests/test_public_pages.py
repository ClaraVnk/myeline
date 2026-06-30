"""Tests for public pages — home, base static routes."""


def test_welcome_page(client):
    r = client.get("/welcome")
    assert r.status_code == 200


def test_reject_cookies_sets_cookie(client):
    # Bug M15 — endpoint passé en POST (CSRF désactivé en tests).
    r = client.post("/reject-cookies")
    assert r.status_code == 204
    assert b"cookie_consent" in r.headers.get("Set-Cookie", "").encode()


def test_accept_cookies_sets_cookie(client):
    r = client.post("/accept-cookies")
    assert r.status_code == 204
    assert b"cookie_consent" in r.headers.get("Set-Cookie", "").encode()


def test_contact_get(client):
    r = client.get("/contact")
    assert r.status_code == 200


def test_health_check(client):
    r = client.get("/health")
    assert r.status_code in (200, 503)
