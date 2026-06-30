"""Extra coverage for app.services.user_scraper_service.

Targets the high-volume missing branches: detect_source, _try_rss
autodiscover, _extract_date variants, _try_sitemap, _index_article
rejection paths, scrape_all_users, delete_scraper_from_index.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests
from bs4 import BeautifulSoup

from app.services import user_scraper_service as svc
from app.utils.safe_http import SSRFBlockedError


# ---------------------------------------------------------------------------
# _try_rss — error / autodiscover paths
# ---------------------------------------------------------------------------


def _resp(content=b"", text="", headers=None, status=200):
    r = MagicMock()
    r.content = content
    r.text = text or content.decode("utf-8", errors="replace")
    r.headers = headers or {}
    r.status_code = status
    r.raise_for_status = MagicMock()
    return r


def test_try_rss_ssrf_returns_none(monkeypatch):
    """SSRFBlockedError on initial fetch → _try_rss returns None."""
    monkeypatch.setattr(svc, "safe_get", MagicMock(side_effect=SSRFBlockedError("blocked")))
    assert svc._try_rss("https://e.example/") is None


def test_try_rss_request_exception_returns_none(monkeypatch):
    """A RequestException on initial fetch → _try_rss returns None."""
    monkeypatch.setattr(svc, "safe_get", MagicMock(side_effect=requests.RequestException("nope")))
    assert svc._try_rss("https://e.example/") is None


def test_try_rss_autodiscover_finds_alternate(monkeypatch):
    """When the initial page is HTML with <link rel=alternate type=rss>, that feed wins."""
    page_html = (
        '<html><head>'
        '<link rel="alternate" type="application/rss+xml" href="/feed.xml" />'
        '</head><body></body></html>'
    )
    initial = _resp(content=page_html.encode(), text=page_html,
                    headers={"Content-Type": "text/html"})
    feed_resp = _resp(content=b"<rss/>", headers={"Content-Type": "application/rss+xml"})

    def fake_safe_get(url, **_):
        if url.endswith("feed.xml"):
            return feed_resp
        return initial

    monkeypatch.setattr(svc, "safe_get", fake_safe_get)

    parses = []

    def fake_parse(arg):
        parses.append(arg)
        # First call: initial page → no entries
        # Second call: feed.xml → entries
        if len(parses) == 1:
            return SimpleNamespace(entries=[])
        return SimpleNamespace(entries=[1])

    monkeypatch.setattr(svc.feedparser, "parse", fake_parse)

    result = svc._try_rss("https://example.com/")
    assert result and result.endswith("/feed.xml")


def test_try_rss_autodiscover_ssrf_skips(monkeypatch):
    """SSRF refusal on an autodiscovered alternate is logged and skipped."""
    page_html = (
        '<html><head>'
        '<link rel="alternate" type="application/rss+xml" href="https://elsewhere/feed" />'
        '</head></html>'
    )
    initial = _resp(content=page_html.encode(), text=page_html,
                    headers={"Content-Type": "text/html"})

    def fake_safe_get(url, **_):
        if url == "https://example.com/":
            return initial
        raise SSRFBlockedError("nope")

    monkeypatch.setattr(svc, "safe_get", fake_safe_get)
    monkeypatch.setattr(svc.feedparser, "parse", lambda _b: SimpleNamespace(entries=[]))

    result = svc._try_rss("https://example.com/")
    # Must not raise; final result is None because no candidate succeeded.
    assert result is None


# ---------------------------------------------------------------------------
# detect_source — RSS path, HTML path, error path
# ---------------------------------------------------------------------------


def test_detect_source_returns_rss(monkeypatch):
    """detect_source returns the rss-feed URL when _try_rss finds one."""
    monkeypatch.setattr(svc, "_try_rss", lambda u: u + "feed.xml")
    url, kind = svc.detect_source("example.com/")
    assert kind == "rss"
    assert url.endswith("feed.xml")


def test_detect_source_returns_html(monkeypatch):
    """A non-RSS reachable HTML page is classified as 'html'."""
    monkeypatch.setattr(svc, "_try_rss", lambda u: None)
    monkeypatch.setattr(svc, "safe_get", MagicMock(return_value=_resp(
        text="<html></html>", headers={"Content-Type": "text/html"})))
    url, kind = svc.detect_source("https://example.com/")
    assert kind == "html"


def test_detect_source_unsupported_content_type(monkeypatch):
    """Non-HTML content types are rejected with ValueError."""
    monkeypatch.setattr(svc, "_try_rss", lambda u: None)
    monkeypatch.setattr(svc, "safe_get", MagicMock(return_value=_resp(
        headers={"Content-Type": "application/octet-stream"})))
    with pytest.raises(ValueError, match="n'est pas supporté"):
        svc.detect_source("https://example.com/")


def test_detect_source_request_exception(monkeypatch):
    """RequestException → ValueError 'URL inaccessible'."""
    monkeypatch.setattr(svc, "_try_rss", lambda u: None)
    monkeypatch.setattr(svc, "safe_get", MagicMock(side_effect=requests.RequestException("dns")))
    with pytest.raises(ValueError, match="URL inaccessible"):
        svc.detect_source("https://example.com/")


def test_detect_source_prepends_scheme(monkeypatch):
    """Bare domain without scheme is normalised to https://."""
    captured = {}

    def fake_try(u):
        captured["url"] = u
        return None

    monkeypatch.setattr(svc, "_try_rss", fake_try)
    monkeypatch.setattr(svc, "safe_get", MagicMock(return_value=_resp(
        headers={"Content-Type": "text/html"})))
    svc.detect_source("example.com")
    assert captured["url"].startswith("https://")


# ---------------------------------------------------------------------------
# _extract_date — meta og, json-ld, <time> branches
# ---------------------------------------------------------------------------


def test_extract_date_from_og_meta():
    """article:published_time meta tag yields a datetime."""
    soup = BeautifulSoup(
        '<meta property="article:published_time" content="2025-01-02T10:00:00Z">',
        "html.parser",
    )
    out = svc._extract_date(soup)
    assert isinstance(out, datetime) and out.year == 2025


def test_extract_date_from_dc_meta():
    """Dublin-Core dc-style meta name yields a datetime."""
    soup = BeautifulSoup(
        '<meta name="DC.date.issued" content="2024-06-15T00:00:00+00:00">',
        "html.parser",
    )
    out = svc._extract_date(soup)
    assert out and out.year == 2024


def test_extract_date_from_json_ld_list():
    """JSON-LD as a list with datePublished is supported."""
    soup = BeautifulSoup(
        '<script type="application/ld+json">'
        '[{"datePublished":"2026-03-10T12:00:00Z"}]'
        '</script>',
        "html.parser",
    )
    out = svc._extract_date(soup)
    assert out and out.year == 2026


def test_extract_date_from_time_tag():
    """A <time datetime="..."> element is consumed when nothing else matches."""
    soup = BeautifulSoup(
        '<time datetime="2023-07-01T10:00:00+00:00">July 1</time>',
        "html.parser",
    )
    out = svc._extract_date(soup)
    assert out and out.year == 2023


def test_extract_date_returns_none_when_absent():
    """Pages without any date markers return None."""
    soup = BeautifulSoup("<html><body><p>x</p></body></html>", "html.parser")
    assert svc._extract_date(soup) is None


def test_extract_date_invalid_iso_fallthrough():
    """Garbage date strings are skipped without raising."""
    soup = BeautifulSoup(
        '<meta property="article:published_time" content="not-a-date">'
        '<meta name="DC.date.issued" content="also-bad">'
        '<time datetime="x">x</time>',
        "html.parser",
    )
    assert svc._extract_date(soup) is None


# ---------------------------------------------------------------------------
# _try_sitemap
# ---------------------------------------------------------------------------


def test_try_sitemap_returns_urls_from_regular_sitemap(monkeypatch):
    """A regular <urlset>-style sitemap returns matching domain URLs."""
    sitemap_xml = (
        '<?xml version="1.0"?><urlset>'
        '<url><loc>https://example.com/articles/2025/post-a</loc></url>'
        '<url><loc>https://other.com/post-b</loc></url>'
        '</urlset>'
    )
    monkeypatch.setattr(svc, "_fetch_raw", lambda url, timeout, retries=1:
                        _resp(text=sitemap_xml, headers={"Content-Type": "application/xml"}))
    out = svc._try_sitemap("https://example.com/", "example.com")
    assert any("post-a" in u for u in out)
    assert not any("other.com" in u for u in out)


def test_try_sitemap_returns_empty_on_no_response(monkeypatch):
    """When all sitemap candidates fail, returns empty list."""
    monkeypatch.setattr(svc, "_fetch_raw", lambda *_a, **_k: None)
    assert svc._try_sitemap("https://example.com/", "example.com") == []


def test_try_sitemap_skips_non_xml(monkeypatch):
    """Sitemap candidate without xml content type and no <?xml prologue is skipped."""
    monkeypatch.setattr(svc, "_fetch_raw", lambda *_a, **_k:
                        _resp(text="not xml", headers={"Content-Type": "text/html"}))
    assert svc._try_sitemap("https://example.com/", "example.com") == []


# ---------------------------------------------------------------------------
# _index_article — quality gates
# ---------------------------------------------------------------------------


def test_index_article_rejects_low_content():
    """Articles below _MIN_CONTENT_CHARS are rejected without DB write."""
    db = MagicMock()
    out = svc._index_article(
        scraper_id=1, user_id=1, scraper_name="x", url="https://e/x",
        title="Title", content="too short", published_at=None,
        collection=MagicMock(), db=db, UserScraperArticle=MagicMock(),
        chunk_text=lambda t: [t], _embed=MagicMock(),
    )
    assert out is False
    db.session.add.assert_not_called()


def test_index_article_rejects_title_equals_url():
    """Skip when title is just the URL (listing/nav page)."""
    db = MagicMock()
    out = svc._index_article(
        scraper_id=1, user_id=1, scraper_name="x",
        url="https://e/foo", title="https://e/foo",
        content="x" * 500, published_at=None,
        collection=MagicMock(), db=db, UserScraperArticle=MagicMock(),
        chunk_text=lambda t: [t], _embed=MagicMock(),
    )
    assert out is False
    db.session.add.assert_not_called()


# ---------------------------------------------------------------------------
# delete_scraper_from_index, scrape_all_for_user, scrape_all_users, _scraper_is_due
# ---------------------------------------------------------------------------


def test_delete_scraper_from_index_no_articles(app, monkeypatch):
    """No-op when the scraper has no articles."""
    fake_query = MagicMock()
    fake_query.filter_by.return_value.all.return_value = []
    fake_collection = MagicMock()
    monkeypatch.setattr(
        "app.models.user_scraper.UserScraperArticle.query", fake_query, raising=False
    )
    monkeypatch.setattr(
        "app.services.rag_service._get_user_collection", lambda _uid: fake_collection
    )
    with app.app_context():
        svc.delete_scraper_from_index(1, user_id=2)
    fake_collection.delete.assert_not_called()


def test_delete_scraper_from_index_swallows_chroma_error(app, monkeypatch):
    """A Chroma delete failure is logged but never raised."""
    fake_article = MagicMock(id=10, chunk_count=2)
    fake_query = MagicMock()
    fake_query.filter_by.return_value.all.return_value = [fake_article]
    fake_collection = MagicMock()
    fake_collection.delete.side_effect = RuntimeError("chroma down")
    monkeypatch.setattr(
        "app.models.user_scraper.UserScraperArticle.query", fake_query, raising=False
    )
    monkeypatch.setattr(
        "app.services.rag_service._get_user_collection", lambda _uid: fake_collection
    )
    with app.app_context():
        svc.delete_scraper_from_index(1, user_id=2)


def test_scraper_is_due_no_last_run():
    """Never-run scrapers are due."""
    s = SimpleNamespace(scrape_frequency="daily", last_scraped_at=None)
    assert svc._scraper_is_due(s) is True


def test_scraper_is_due_recent_run_not_due():
    """A scraper run a minute ago at daily cadence isn't due."""
    s = SimpleNamespace(
        scrape_frequency="daily",
        last_scraped_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    assert svc._scraper_is_due(s) is False


def test_scrape_all_users_skips_when_not_due(app, monkeypatch):
    """A scraper that's not due increments 'skipped' rather than running."""
    user = SimpleNamespace(id=1)
    fake_user_q = MagicMock()
    fake_user_q.all.return_value = [user]

    scraper = MagicMock(id=99)
    fake_scraper_q = MagicMock()
    fake_scraper_q.filter_by.return_value.all.return_value = [scraper]

    monkeypatch.setattr("app.models.user.User.query", fake_user_q, raising=False)
    monkeypatch.setattr(
        "app.models.user_scraper.UserScraper.query", fake_scraper_q, raising=False
    )
    monkeypatch.setattr(svc, "_scraper_is_due", lambda s: False)
    monkeypatch.setattr(svc, "scrape_source", lambda _id: (0, None))

    with app.app_context():
        out = svc.scrape_all_users()
    assert out["skipped"] == 1
    assert out["users"] == 0


def test_scrape_all_users_counts_errors(app, monkeypatch):
    """An error string from scrape_source increments the errors counter."""
    user = SimpleNamespace(id=1)
    fake_user_q = MagicMock()
    fake_user_q.all.return_value = [user]

    scraper = MagicMock(id=42)
    fake_scraper_q = MagicMock()
    fake_scraper_q.filter_by.return_value.all.return_value = [scraper]

    monkeypatch.setattr("app.models.user.User.query", fake_user_q, raising=False)
    monkeypatch.setattr(
        "app.models.user_scraper.UserScraper.query", fake_scraper_q, raising=False
    )
    monkeypatch.setattr(svc, "_scraper_is_due", lambda s: True)
    monkeypatch.setattr(svc, "scrape_source", lambda _id: (3, "boom"))

    with app.app_context():
        out = svc.scrape_all_users()
    assert out["errors"] == 1
    assert out["total_new_articles"] == 3
    assert out["users"] == 1


def test_scrape_all_users_no_scrapers_skips_user(app, monkeypatch):
    """A user with no scrapers does not increment any counter."""
    user = SimpleNamespace(id=1)
    fake_user_q = MagicMock()
    fake_user_q.all.return_value = [user]

    fake_scraper_q = MagicMock()
    fake_scraper_q.filter_by.return_value.all.return_value = []

    monkeypatch.setattr("app.models.user.User.query", fake_user_q, raising=False)
    monkeypatch.setattr(
        "app.models.user_scraper.UserScraper.query", fake_scraper_q, raising=False
    )

    with app.app_context():
        out = svc.scrape_all_users()
    assert out == {"users": 0, "total_new_articles": 0, "errors": 0, "skipped": 0}
