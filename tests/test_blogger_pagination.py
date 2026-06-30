"""Blogger feeds cap at 25 entries/page and never expose rel="next" —
verify our paginator switches to the ?start-index= cursor so a user
who selects a since-date actually gets the backlog they asked for.
"""
from unittest.mock import MagicMock, patch


def _make_resp(content: bytes):
    r = MagicMock()
    r.content = content
    return r


def test_blogger_pagination_url_adds_max_results():
    from app.services.user_scraper_service import _blogger_pagination_url
    url = _blogger_pagination_url(
        "https://www.medicalement-geek.com/feeds/posts/default",
        start_index=1,
    )
    assert url is not None
    assert "max-results=500" in url
    assert "start-index=1" in url


def test_blogger_pagination_url_preserves_existing_query():
    from app.services.user_scraper_service import _blogger_pagination_url
    url = _blogger_pagination_url(
        "https://b.example.com/feeds/posts/default?alt=atom",
        start_index=501,
    )
    assert "alt=atom" in url
    assert "start-index=501" in url
    assert "max-results=500" in url


def test_blogger_pagination_url_skips_non_blogger():
    from app.services.user_scraper_service import _blogger_pagination_url
    assert _blogger_pagination_url("https://wp.example.com/feed/", 1) is None


def test_blogger_pagination_url_keeps_explicit_max_results():
    """User-provided max-results in the URL must not be overwritten."""
    from app.services.user_scraper_service import _blogger_pagination_url
    url = _blogger_pagination_url(
        "https://b.example.com/feeds/posts/default?max-results=50",
        start_index=1,
    )
    assert "max-results=50" in url
    assert "max-results=500" not in url


def test_fetch_atom_pages_blogger_walks_cursor():
    """Three fake Blogger pages: 500, 500, 100 entries → 1100 total."""
    from app.services import user_scraper_service as svc

    page1 = MagicMock(entries=[MagicMock() for _ in range(500)], feed=MagicMock(links=[]))
    page2 = MagicMock(entries=[MagicMock() for _ in range(500)], feed=MagicMock(links=[]))
    page3 = MagicMock(entries=[MagicMock() for _ in range(100)], feed=MagicMock(links=[]))
    parse_calls = []

    def fake_parse(content):
        parse_calls.append(content)
        return [page1, page2, page3][len(parse_calls) - 1]

    fake_safe_get = MagicMock(side_effect=lambda url, **kw: _make_resp(url.encode()))
    with patch.object(svc, "safe_get", fake_safe_get), \
         patch.object(svc, "feedparser") as fp:
        fp.parse.side_effect = fake_parse
        entries, err = svc._fetch_atom_pages(
            "https://blog.example.com/feeds/posts/default",
            since=None,
        )

    assert err is None
    assert len(entries) == 1100
    # Three HTTP requests, each with growing start-index.
    urls = [c.args[0] for c in fake_safe_get.call_args_list]
    assert "start-index=1" in urls[0] and "max-results=500" in urls[0]
    assert "start-index=501" in urls[1]
    assert "start-index=1001" in urls[2]


def test_fetch_atom_pages_blogger_stops_on_short_page():
    """A short first page (< 500) means the blog has fewer posts — stop."""
    from app.services import user_scraper_service as svc

    page1 = MagicMock(entries=[MagicMock() for _ in range(141)], feed=MagicMock(links=[]))

    def fake_parse(content):
        return page1

    fake_safe_get = MagicMock(side_effect=lambda url, **kw: _make_resp(url.encode()))
    with patch.object(svc, "safe_get", fake_safe_get), \
         patch.object(svc, "feedparser") as fp:
        fp.parse.side_effect = fake_parse
        entries, err = svc._fetch_atom_pages(
            "https://blog.example.com/feeds/posts/default",
            since=None,
        )

    assert err is None
    assert len(entries) == 141
    assert fake_safe_get.call_count == 1  # no follow-up paging
