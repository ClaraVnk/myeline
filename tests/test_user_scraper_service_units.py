"""Coverage for app/services/user_scraper_service.py — pure helpers + mocks.

The full scrape pipeline needs the network and a real DB; this file tests
the parsers, URL classifiers, retry logic, and the frequency / cap helpers
that don't need either.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────
# _url_hash
# ─────────────────────────────────────────────────────────────────

class TestUrlHash:
    def test_deterministic(self):
        from app.services.user_scraper_service import _url_hash
        assert _url_hash("https://example.com/a") == _url_hash("https://example.com/a")

    def test_different_urls_distinct(self):
        from app.services.user_scraper_service import _url_hash
        a = _url_hash("https://example.com/a")
        b = _url_hash("https://example.com/b")
        assert a != b

    def test_returns_64_hex_chars(self):
        from app.services.user_scraper_service import _url_hash
        h = _url_hash("anything")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ─────────────────────────────────────────────────────────────────
# _parse_entry_content — feedparser entry duck-typing
# ─────────────────────────────────────────────────────────────────

class TestParseEntryContent:
    def test_prefers_content_field(self):
        from app.services.user_scraper_service import _parse_entry_content
        entry = SimpleNamespace(
            content=[{"value": "from-content"}],
            summary="from-summary",
            description="from-description",
        )
        assert _parse_entry_content(entry) == "from-content"

    def test_falls_back_to_summary(self):
        from app.services.user_scraper_service import _parse_entry_content
        entry = SimpleNamespace(summary="from-summary", description="from-description")
        # entry has no .content
        assert _parse_entry_content(entry) == "from-summary"

    def test_falls_back_to_description(self):
        from app.services.user_scraper_service import _parse_entry_content
        entry = SimpleNamespace(description="from-description")
        assert _parse_entry_content(entry) == "from-description"

    def test_returns_empty_when_nothing_present(self):
        from app.services.user_scraper_service import _parse_entry_content
        entry = SimpleNamespace()
        assert _parse_entry_content(entry) == ""

    def test_handles_empty_content_list(self):
        from app.services.user_scraper_service import _parse_entry_content
        entry = SimpleNamespace(content=[], summary="fallback")
        # empty content list is falsy → falls back to summary
        assert _parse_entry_content(entry) == "fallback"


# ─────────────────────────────────────────────────────────────────
# _base_domain
# ─────────────────────────────────────────────────────────────────

class TestBaseDomain:
    def test_strips_www_prefix(self):
        from app.services.user_scraper_service import _base_domain
        assert _base_domain("www.example.com") == "example.com"

    def test_no_www_unchanged(self):
        from app.services.user_scraper_service import _base_domain
        assert _base_domain("example.com") == "example.com"

    def test_lowercases_input(self):
        from app.services.user_scraper_service import _base_domain
        assert _base_domain("WWW.Example.COM") == "example.com"


# ─────────────────────────────────────────────────────────────────
# _is_article_link
# ─────────────────────────────────────────────────────────────────

class TestIsArticleLink:
    @pytest.mark.parametrize("href,base_netloc,expected", [
        # OK: same domain + path with at least one slash
        ("https://example.com/posts/my-article", "example.com", True),
        ("https://www.example.com/blog/foo", "example.com", True),
        # Reject: different domain
        ("https://other.com/posts/article", "example.com", False),
        # Reject: non-http(s) scheme
        ("ftp://example.com/article", "example.com", False),
        ("javascript:void(0)", "example.com", False),
        # Reject: skip segments (tag, category, page, login, search...)
        ("https://example.com/tag/python", "example.com", False),
        ("https://example.com/category/news", "example.com", False),
        ("https://example.com/login", "example.com", False),
        # Reject: file extensions
        ("https://example.com/files/doc.pdf", "example.com", False),
        ("https://example.com/img/photo.jpg", "example.com", False),
        # Reject: root path
        ("https://example.com/", "example.com", False),
        ("https://example.com", "example.com", False),
    ])
    def test_classification(self, href, base_netloc, expected):
        from app.services.user_scraper_service import _is_article_link
        assert _is_article_link(href, base_netloc) is expected


# ─────────────────────────────────────────────────────────────────
# _scraper_is_due / _FREQUENCY_SECONDS
# ─────────────────────────────────────────────────────────────────

class TestScraperIsDue:
    def test_never_run_is_due(self):
        from app.services.user_scraper_service import _scraper_is_due
        scraper = SimpleNamespace(scrape_frequency="daily", last_scraped_at=None)
        assert _scraper_is_due(scraper) is True

    def test_daily_freshly_run_not_due(self):
        from app.services.user_scraper_service import _scraper_is_due
        scraper = SimpleNamespace(
            scrape_frequency="daily",
            last_scraped_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        assert _scraper_is_due(scraper) is False

    def test_daily_after_25h_is_due(self):
        from app.services.user_scraper_service import _scraper_is_due
        scraper = SimpleNamespace(
            scrape_frequency="daily",
            last_scraped_at=datetime.now(timezone.utc) - timedelta(hours=25),
        )
        assert _scraper_is_due(scraper) is True

    def test_weekly_after_3_days_not_due(self):
        from app.services.user_scraper_service import _scraper_is_due
        scraper = SimpleNamespace(
            scrape_frequency="weekly",
            last_scraped_at=datetime.now(timezone.utc) - timedelta(days=3),
        )
        assert _scraper_is_due(scraper) is False

    def test_weekly_after_8_days_is_due(self):
        from app.services.user_scraper_service import _scraper_is_due
        scraper = SimpleNamespace(
            scrape_frequency="weekly",
            last_scraped_at=datetime.now(timezone.utc) - timedelta(days=8),
        )
        assert _scraper_is_due(scraper) is True

    def test_unknown_frequency_defaults_to_daily(self):
        from app.services.user_scraper_service import _scraper_is_due
        scraper = SimpleNamespace(
            scrape_frequency="weird",
            last_scraped_at=datetime.now(timezone.utc) - timedelta(hours=25),
        )
        assert _scraper_is_due(scraper) is True

    def test_none_frequency_defaults_to_daily(self):
        from app.services.user_scraper_service import _scraper_is_due
        scraper = SimpleNamespace(
            scrape_frequency=None,
            last_scraped_at=datetime.now(timezone.utc) - timedelta(hours=23),
        )
        assert _scraper_is_due(scraper) is False


# ─────────────────────────────────────────────────────────────────
# _fetch_raw — retry behaviour
# ─────────────────────────────────────────────────────────────────

class TestFetchRaw:
    def test_returns_response_on_2xx(self):
        from app.services.user_scraper_service import _fetch_raw
        ok = MagicMock(status_code=200)
        ok.raise_for_status = MagicMock()
        with patch("app.services.user_scraper_service.safe_get", return_value=ok):
            r = _fetch_raw("https://example.com", timeout=5)
        assert r is ok

    def test_retries_on_429(self):
        from app.services.user_scraper_service import _fetch_raw
        rate_limited = MagicMock(status_code=429)
        rate_limited.headers = {"Retry-After": "1"}
        ok = MagicMock(status_code=200)
        ok.raise_for_status = MagicMock()
        with patch(
            "app.services.user_scraper_service.safe_get",
            side_effect=[rate_limited, ok],
        ):
            with patch("app.services.user_scraper_service.time.sleep"):
                r = _fetch_raw("https://example.com", timeout=5, retries=1)
        assert r is ok

    def test_retries_on_5xx(self):
        from app.services.user_scraper_service import _fetch_raw
        server_err = MagicMock(status_code=503)
        server_err.raise_for_status = MagicMock()
        ok = MagicMock(status_code=200)
        ok.raise_for_status = MagicMock()
        with patch(
            "app.services.user_scraper_service.safe_get",
            side_effect=[server_err, ok],
        ):
            with patch("app.services.user_scraper_service.time.sleep"):
                r = _fetch_raw("https://example.com", timeout=5, retries=1)
        assert r is ok

    def test_returns_none_after_request_exception(self):
        import requests as _req

        from app.services.user_scraper_service import _fetch_raw
        with patch(
            "app.services.user_scraper_service.safe_get",
            side_effect=_req.Timeout("timeout"),
        ):
            with patch("app.services.user_scraper_service.time.sleep"):
                r = _fetch_raw("https://example.com", timeout=5, retries=1)
        assert r is None


# ─────────────────────────────────────────────────────────────────
# _extract_date — meta tags + JSON-LD + <time>
# ─────────────────────────────────────────────────────────────────

class TestExtractDate:
    def test_open_graph_meta(self):
        from bs4 import BeautifulSoup

        from app.services.user_scraper_service import _extract_date
        html = '<meta property="article:published_time" content="2025-06-15T10:00:00Z">'
        soup = BeautifulSoup(html, "html.parser")
        d = _extract_date(soup)
        assert d is not None
        assert d.year == 2025 and d.month == 6 and d.day == 15

    def test_jsonld_date_published(self):
        from bs4 import BeautifulSoup

        from app.services.user_scraper_service import _extract_date
        html = (
            '<script type="application/ld+json">'
            '{"datePublished": "2024-12-01T08:30:00Z"}'
            '</script>'
        )
        soup = BeautifulSoup(html, "html.parser")
        d = _extract_date(soup)
        assert d is not None
        assert d.year == 2024 and d.month == 12

    def test_time_tag_datetime(self):
        from bs4 import BeautifulSoup

        from app.services.user_scraper_service import _extract_date
        html = '<time datetime="2023-03-10T14:00:00Z">10 Mars</time>'
        soup = BeautifulSoup(html, "html.parser")
        d = _extract_date(soup)
        assert d is not None
        assert d.year == 2023 and d.month == 3

    def test_no_metadata_returns_none(self):
        from bs4 import BeautifulSoup

        from app.services.user_scraper_service import _extract_date
        soup = BeautifulSoup("<p>just text</p>", "html.parser")
        assert _extract_date(soup) is None

    def test_invalid_iso_format_skipped(self):
        from bs4 import BeautifulSoup

        from app.services.user_scraper_service import _extract_date
        html = '<meta property="article:published_time" content="not-a-date">'
        soup = BeautifulSoup(html, "html.parser")
        assert _extract_date(soup) is None


# ─────────────────────────────────────────────────────────────────
# _try_rss — feedparser-driven
# ─────────────────────────────────────────────────────────────────

class TestTryRss:
    def test_direct_feed_url_recognised(self):
        from app.services.user_scraper_service import _try_rss

        # safe_get returns valid feed bytes; feedparser confirms entries.
        fake_resp = MagicMock(content=b"<rss/>",
                              text="<rss/>",
                              headers={"Content-Type": "application/rss+xml"})
        with patch("app.services.user_scraper_service.safe_get",
                   return_value=fake_resp):
            with patch("app.services.user_scraper_service.feedparser.parse") as fp:
                fp.return_value = SimpleNamespace(entries=[{"title": "x"}])
                assert _try_rss("https://blog.example.com/feed") == "https://blog.example.com/feed"

    def test_returns_none_when_no_feed_found(self):
        from app.services.user_scraper_service import _try_rss

        # safe_get returns a non-feed HTML page; feedparser returns no
        # entries; HTML autodiscovery finds nothing → None.
        fake_resp = MagicMock(content=b"<html/>",
                              text="<html/>",
                              headers={"Content-Type": "text/plain"})
        with patch(
            "app.services.user_scraper_service.feedparser.parse",
            return_value=SimpleNamespace(entries=[]),
        ):
            with patch(
                "app.services.user_scraper_service.safe_get",
                return_value=fake_resp,
            ):
                assert _try_rss("https://example.com") is None
