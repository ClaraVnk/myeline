"""Tests for scraper RSS feed discovery endpoint."""
from unittest.mock import patch

from tests.helpers import login


def _login_subscriber(client):
    login(client, "subscriber@example.com", "Alice1234!")


# ── Auth guard ────────────────────────────────────────────────────────────────


def test_discover_feed_requires_login(client):
    r = client.get("/user/mes-sources/discover-feed?url=https://example.com", follow_redirects=False)
    assert r.status_code in (301, 302)


# ── Feed discovery ────────────────────────────────────────────────────────────


def test_discover_feed_missing_url(client, subscriber_user):
    _login_subscriber(client)
    r = client.get("/user/mes-sources/discover-feed")
    assert r.status_code == 400
    client.get("/logout")


def test_discover_feed_found(client, subscriber_user):
    _login_subscriber(client)
    with patch(
        "app.routes.user_scrapers._try_rss",
        return_value="https://example.com/feed.xml",
    ):
        r = client.get("/user/mes-sources/discover-feed?url=https://example.com")
    assert r.status_code == 200
    data = r.get_json()
    assert data["found"] is True
    assert data["feed_url"] == "https://example.com/feed.xml"
    client.get("/logout")


def test_discover_feed_not_found(client, subscriber_user):
    _login_subscriber(client)
    with patch("app.routes.user_scrapers._try_rss", return_value=None):
        r = client.get("/user/mes-sources/discover-feed?url=https://example.com")
    assert r.status_code == 200
    data = r.get_json()
    assert data["found"] is False
    client.get("/logout")


def test_discover_feed_prepends_https(client, subscriber_user):
    """URL without scheme gets https:// prepended."""
    _login_subscriber(client)
    with patch("app.routes.user_scrapers._try_rss", return_value=None) as mock:
        r = client.get("/user/mes-sources/discover-feed?url=example.com")
    assert r.status_code == 200
    mock.assert_called_once_with("https://example.com")
    client.get("/logout")
