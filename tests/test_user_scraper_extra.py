"""Coverage for app/services/user_scraper_service.py — focused on the
heavyweight paths missed by existing test files: _find_article_links,
_fetch_article, _index_article, scrape_source dispatcher, and the
RSS/HTML run helpers.

HTTP and RAG embedding boundaries are mocked.
"""
import uuid
from unittest.mock import MagicMock, patch

import pytest


# ─── _index_article ───────────────────────────────────────────────────

class TestIndexArticle:
    def _setup(self, app, db):
        from app.models.user import User
        from app.models.user_scraper import UserScraper
        u = User(
            email=f"sc-{uuid.uuid4().hex[:6]}@example.com",
            firstname="S", lastname="C",
            is_confirmed=True, is_active=True,
            abonnement_type="premium_pro",
        )
        u.set_password("Sc1234!")
        db.session.add(u)
        db.session.flush()
        s = UserScraper(
            user_id=u.id, name="Test", url="https://example.com",
            scraper_type="rss", is_active=True,
        )
        db.session.add(s)
        db.session.commit()
        return u, s

    def test_skips_low_content(self, app, db):
        from app.models.user_scraper import UserScraperArticle
        from app.services.user_scraper_service import _index_article
        with app.app_context():
            u, s = self._setup(app, db)
            collection = MagicMock()
            ok = _index_article(
                scraper_id=s.id, user_id=u.id, scraper_name=s.name,
                url="https://x.com/a", title="Title",
                content="x" * 50,  # below _MIN_CONTENT_CHARS = 150
                published_at=None,
                collection=collection, db=db,
                UserScraperArticle=UserScraperArticle,
                chunk_text=lambda t: [t], _embed=lambda chunks, doc_title=None, user_id=None: [[0.0]],
            )
        assert ok is False
        collection.upsert.assert_not_called()

    def test_skips_when_title_equals_url(self, app, db):
        from app.models.user_scraper import UserScraperArticle
        from app.services.user_scraper_service import _index_article
        with app.app_context():
            u, s = self._setup(app, db)
            collection = MagicMock()
            url = "https://x.com/a/"
            ok = _index_article(
                scraper_id=s.id, user_id=u.id, scraper_name=s.name,
                url=url, title="https://x.com/a",  # same as URL
                content="A" * 500,
                published_at=None,
                collection=collection, db=db,
                UserScraperArticle=UserScraperArticle,
                chunk_text=lambda t: [t], _embed=lambda chunks, doc_title=None, user_id=None: [[0.0]],
            )
        assert ok is False

    def test_indexes_valid_article(self, app, db):
        from app.models.user_scraper import UserScraperArticle
        from app.services.user_scraper_service import _index_article
        with app.app_context():
            u, s = self._setup(app, db)
            collection = MagicMock()
            collection.upsert = MagicMock()
            ok = _index_article(
                scraper_id=s.id, user_id=u.id, scraper_name=s.name,
                url="https://x.com/article", title="Real article",
                content="Real article content. " * 30,
                published_at=None,
                collection=collection, db=db,
                UserScraperArticle=UserScraperArticle,
                chunk_text=lambda t: [t[:500], t[500:1000]],
                _embed=lambda chunks, doc_title=None, user_id=None: [[0.1] for _ in chunks],
            )
            row = UserScraperArticle.query.filter_by(scraper_id=s.id).first()
        assert ok is True
        assert collection.upsert.called
        assert row is not None
        assert row.chunk_count == 2
        assert row.indexed_at is not None

    def test_swallows_embedding_failure(self, app, db):
        from app.models.user_scraper import UserScraperArticle
        from app.services.user_scraper_service import _index_article
        with app.app_context():
            u, s = self._setup(app, db)
            collection = MagicMock()
            ok = _index_article(
                scraper_id=s.id, user_id=u.id, scraper_name=s.name,
                url="https://x.com/article", title="Real article",
                content="Real article content. " * 30,
                published_at=None,
                collection=collection, db=db,
                UserScraperArticle=UserScraperArticle,
                chunk_text=lambda t: [t],
                _embed=MagicMock(side_effect=RuntimeError("embed down")),
            )
        assert ok is False


# ─── _extract_date ────────────────────────────────────────────────────

class TestExtractDateExtra:
    def test_meta_published_time(self):
        from bs4 import BeautifulSoup

        from app.services.user_scraper_service import _extract_date
        html = '<html><head><meta property="article:published_time" content="2026-01-15T10:00:00Z"></head></html>'
        d = _extract_date(BeautifulSoup(html, "lxml"))
        assert d is not None
        assert d.year == 2026 and d.month == 1 and d.day == 15

    def test_jsonld_datePublished(self):
        from bs4 import BeautifulSoup

        from app.services.user_scraper_service import _extract_date
        html = """<html><head><script type="application/ld+json">
        {"datePublished": "2026-02-01T00:00:00Z"}
        </script></head></html>"""
        d = _extract_date(BeautifulSoup(html, "lxml"))
        assert d is not None
        assert d.month == 2

    def test_time_element_with_datetime(self):
        from bs4 import BeautifulSoup

        from app.services.user_scraper_service import _extract_date
        html = '<html><body><time datetime="2026-03-10"></time></body></html>'
        d = _extract_date(BeautifulSoup(html, "lxml"))
        assert d is not None
        assert d.month == 3

    def test_returns_none_when_no_date(self):
        from bs4 import BeautifulSoup

        from app.services.user_scraper_service import _extract_date
        html = "<html><body><p>no date</p></body></html>"
        assert _extract_date(BeautifulSoup(html, "lxml")) is None


# ─── _find_article_links ──────────────────────────────────────────────

class TestFindArticleLinks:
    def _resp(self, html: str):
        r = MagicMock()
        r.text = html
        r.headers = {"Content-Type": "text/html"}
        return r

    def test_returns_empty_when_fetch_fails(self):
        from app.services import user_scraper_service
        with patch.object(user_scraper_service, "_fetch_raw", return_value=None):
            assert user_scraper_service._find_article_links("https://x.com") == []

    def test_collects_links_from_article_containers(self):
        from app.services import user_scraper_service
        html = """
        <html><body>
          <article>
            <a href="/2026/01/foo">Foo article</a>
            <a href="/2026/01/bar">Bar article</a>
          </article>
          <a href="/about">About</a>
        </body></html>
        """
        with patch.object(user_scraper_service, "_fetch_raw",
                          return_value=self._resp(html)), \
             patch.object(user_scraper_service, "_try_sitemap",
                          return_value=[]):
            links = user_scraper_service._find_article_links("https://example.com/news")
        assert any("foo" in u for u in links)
        assert any("bar" in u for u in links)
        # /about excluded by _SKIP_SEGMENTS heuristics
        assert not any(u.endswith("/about") for u in links)

    def test_falls_back_to_sitemap_when_few_links(self):
        from app.services import user_scraper_service
        html = "<html><body><a href='/x'>X</a></body></html>"
        with patch.object(user_scraper_service, "_fetch_raw",
                          return_value=self._resp(html)), \
             patch.object(user_scraper_service, "_try_sitemap",
                          return_value=["https://example.com/2026/01/sitemap-art"]):
            links = user_scraper_service._find_article_links("https://example.com/")
        assert "https://example.com/2026/01/sitemap-art" in links


# ─── _try_sitemap ─────────────────────────────────────────────────────

class TestTrySitemap:
    def test_returns_empty_when_all_candidates_fail(self):
        from app.services import user_scraper_service
        with patch.object(user_scraper_service, "_fetch_raw", return_value=None):
            assert user_scraper_service._try_sitemap("https://x.com", "x.com") == []

    def test_parses_regular_sitemap(self):
        from app.services import user_scraper_service
        sitemap = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://x.com/2026/01/article-1</loc></url>
          <url><loc>https://x.com/2026/01/article-2</loc></url>
        </urlset>"""
        resp = MagicMock(text=sitemap, headers={"Content-Type": "application/xml"})
        with patch.object(user_scraper_service, "_fetch_raw",
                          side_effect=[resp, None, None]):
            urls = user_scraper_service._try_sitemap("https://x.com", "x.com")
        assert "https://x.com/2026/01/article-1" in urls


# ─── _fetch_article ───────────────────────────────────────────────────

class TestFetchArticle:
    def test_returns_empty_on_fetch_failure(self):
        from app.services import user_scraper_service
        with patch.object(user_scraper_service, "_fetch_raw", return_value=None):
            t, c, d = user_scraper_service._fetch_article("https://x.com/a")
        assert (t, c, d) == ("", "", None)

    def test_extracts_title_and_content(self):
        from app.services import user_scraper_service
        html = """<html>
          <head>
            <meta property="og:title" content="My Article">
            <title>Page</title>
          </head>
          <body>
            <article>
              <p>This is a long enough piece of body text that should pass the
                 minimum-content threshold to be considered valid.</p>
              <p>Another paragraph with substantive content for indexing.</p>
            </article>
          </body>
        </html>"""
        resp = MagicMock(text=html, headers={"Content-Type": "text/html"})
        with patch.object(user_scraper_service, "_fetch_raw", return_value=resp):
            title, content, _ = user_scraper_service._fetch_article("https://x.com/a")
        assert title == "My Article"
        assert "long enough piece" in content

    def test_falls_back_to_meta_description(self):
        """Page with no real body content but a meta description should
        return that description as last-resort content."""
        from app.services import user_scraper_service
        html = """<html>
          <head>
            <title>X</title>
            <meta name="description" content="Description used as fallback content for this short page.">
          </head>
          <body><p>tiny</p></body>
        </html>"""
        resp = MagicMock(text=html, headers={"Content-Type": "text/html"})
        # Bypass trafilatura if installed (returns nothing for short pages anyway)
        with patch.object(user_scraper_service, "_HAS_TRAFILATURA", False), \
             patch.object(user_scraper_service, "_fetch_raw", return_value=resp):
            title, content, _ = user_scraper_service._fetch_article("https://x.com/a")
        assert "Description used as fallback" in content


# ─── scrape_source dispatcher + helpers ──────────────────────────────

class TestScrapeSource:
    def _setup(self, app, db, scraper_type="rss"):
        from app.models.user import User
        from app.models.user_scraper import UserScraper
        u = User(
            email=f"src-{uuid.uuid4().hex[:6]}@example.com",
            is_confirmed=True, is_active=True,
            abonnement_type="premium_pro",
        )
        u.set_password("Src1234!")
        db.session.add(u)
        db.session.flush()
        s = UserScraper(
            user_id=u.id, name="T", url="https://example.com/feed",
            scraper_type=scraper_type, is_active=True,
        )
        db.session.add(s)
        db.session.commit()
        return u, s

    def test_returns_error_when_scraper_inactive(self, app, db):
        from app.services.user_scraper_service import scrape_source
        with app.app_context():
            u, s = self._setup(app, db)
            s.is_active = False
            db.session.commit()
            count, err = scrape_source(s.id)
        assert count == 0
        assert err

    def test_dispatches_to_rss_runner(self, app, db):
        from app.services import user_scraper_service
        with app.app_context():
            u, s = self._setup(app, db, scraper_type="rss")
            with patch.object(user_scraper_service, "_run_rss_scrape",
                              return_value=(3, None)) as mock_rss, \
                 patch.object(user_scraper_service, "_run_html_scrape") as mock_html, \
                 patch("app.services.rag_service._get_user_collection",
                       return_value=MagicMock()), \
                 patch("app.services.rag_service._embed", return_value=[[0.0]]):
                count, err = user_scraper_service.scrape_source(s.id)
        assert count == 3
        mock_rss.assert_called_once()
        mock_html.assert_not_called()

    def test_dispatches_to_html_runner(self, app, db):
        from app.services import user_scraper_service
        with app.app_context():
            u, s = self._setup(app, db, scraper_type="html")
            with patch.object(user_scraper_service, "_run_rss_scrape") as mock_rss, \
                 patch.object(user_scraper_service, "_run_html_scrape",
                              return_value=(7, None)) as mock_html, \
                 patch("app.services.rag_service._get_user_collection",
                       return_value=MagicMock()), \
                 patch("app.services.rag_service._embed", return_value=[[0.0]]):
                count, err = user_scraper_service.scrape_source(s.id)
        assert count == 7
        mock_html.assert_called_once()
        mock_rss.assert_not_called()


# ─── _run_rss_scrape ──────────────────────────────────────────────────

class TestRunRssScrape:
    def _setup(self, app, db):
        from app.models.user import User
        from app.models.user_scraper import UserScraper
        u = User(
            email=f"rss-{uuid.uuid4().hex[:6]}@example.com",
            is_confirmed=True, is_active=True,
            abonnement_type="premium_pro",
        )
        u.set_password("Rss12345!")
        db.session.add(u)
        db.session.flush()
        s = UserScraper(
            user_id=u.id, name="F", url="https://example.com/feed.xml",
            scraper_type="rss", is_active=True,
        )
        db.session.add(s)
        db.session.commit()
        return u, s

    def test_returns_error_on_feed_parse_failure(self, app, db):
        from app.models.user_scraper import UserScraperArticle
        from app.services import user_scraper_service
        fake_resp = MagicMock(content=b"<rss/>",
                              headers={"Content-Type": "application/rss+xml"})
        with app.app_context():
            u, s = self._setup(app, db)
            with patch.object(user_scraper_service, "safe_get",
                              return_value=fake_resp):
                with patch.object(user_scraper_service, "feedparser") as mock_fp:
                    mock_fp.parse.side_effect = Exception("network")
                    count, err = user_scraper_service._run_rss_scrape(
                        scraper=s, user_id=u.id, existing_count=0,
                        max_articles=200, collection=MagicMock(),
                        db=db, UserScraperArticle=UserScraperArticle,
                        chunk_text=lambda t: [t],
                        _embed=lambda chunks, doc_title=None, user_id=None: [[0.0]],
                    )
        assert count == 0
        assert "Erreur" in err

    def test_returns_error_when_feed_empty(self, app, db):
        from app.models.user_scraper import UserScraperArticle
        from app.services import user_scraper_service
        fake_resp = MagicMock(content=b"<rss/>",
                              headers={"Content-Type": "application/rss+xml"})
        with app.app_context():
            u, s = self._setup(app, db)
            fake_feed = MagicMock(entries=[])
            with patch.object(user_scraper_service, "safe_get",
                              return_value=fake_resp):
                with patch.object(user_scraper_service, "feedparser") as mock_fp:
                    mock_fp.parse.return_value = fake_feed
                    count, err = user_scraper_service._run_rss_scrape(
                        scraper=s, user_id=u.id, existing_count=0,
                        max_articles=200, collection=MagicMock(),
                        db=db, UserScraperArticle=UserScraperArticle,
                        chunk_text=lambda t: [t],
                        _embed=lambda chunks, doc_title=None, user_id=None: [[0.0]],
                    )
        assert count == 0
        assert err == "Flux vide ou inaccessible."

    def test_indexes_new_entries_and_skips_known(self, app, db):
        from datetime import datetime, timezone

        from app.models.user_scraper import UserScraperArticle
        from app.services import user_scraper_service
        with app.app_context():
            u, s = self._setup(app, db)

            # Pre-existing article keyed on url_hash → should be skipped.
            from app.services.user_scraper_service import _url_hash
            existing = UserScraperArticle(
                scraper_id=s.id, user_id=u.id,
                url_hash=_url_hash("https://x.com/known"),
                url="https://x.com/known", title="Known",
                content="...", chunk_count=1,
            )
            db.session.add(existing)
            db.session.commit()

            entry_known = MagicMock()
            entry_known.link = "https://x.com/known"
            entry_known.title = "Known"
            entry_known.published_parsed = (2026, 1, 1, 0, 0, 0)

            entry_new = MagicMock()
            entry_new.link = "https://x.com/new"
            entry_new.title = "New"
            entry_new.published_parsed = (2026, 1, 5, 0, 0, 0)
            entry_new.content = [
                {"value": "<p>A reasonably long body for the new article we want indexed.</p>" * 5}
            ]

            fake_feed = MagicMock(entries=[entry_known, entry_new])
            fake_resp = MagicMock(content=b"<rss/>",
                                  headers={"Content-Type": "application/rss+xml"})
            with patch.object(user_scraper_service, "safe_get",
                              return_value=fake_resp):
                with patch.object(user_scraper_service, "feedparser") as mock_fp:
                    mock_fp.parse.return_value = fake_feed
                    count, err = user_scraper_service._run_rss_scrape(
                        scraper=s, user_id=u.id, existing_count=1,
                        max_articles=200, collection=MagicMock(),
                        db=db, UserScraperArticle=UserScraperArticle,
                        chunk_text=lambda t: [t],
                        _embed=lambda chunks, doc_title=None, user_id=None: [[0.0] for _ in chunks],
                    )
        assert err is None
        assert count == 1


# ─── _run_html_scrape ─────────────────────────────────────────────────

class TestRunHtmlScrape:
    def _setup(self, app, db):
        from app.models.user import User
        from app.models.user_scraper import UserScraper
        u = User(
            email=f"html-{uuid.uuid4().hex[:6]}@example.com",
            is_confirmed=True, is_active=True,
            abonnement_type="premium_pro",
        )
        u.set_password("Html1234!")
        db.session.add(u)
        db.session.flush()
        s = UserScraper(
            user_id=u.id, name="H", url="https://example.com/news",
            scraper_type="html", is_active=True,
        )
        db.session.add(s)
        db.session.commit()
        return u, s

    def test_returns_error_when_no_links(self, app, db):
        from app.models.user_scraper import UserScraperArticle
        from app.services import user_scraper_service
        with app.app_context():
            u, s = self._setup(app, db)
            with patch.object(user_scraper_service, "_find_article_links",
                              return_value=[]):
                count, err = user_scraper_service._run_html_scrape(
                    scraper=s, user_id=u.id, existing_count=0,
                    max_articles=200, collection=MagicMock(),
                    db=db, UserScraperArticle=UserScraperArticle,
                    chunk_text=lambda t: [t],
                    _embed=lambda chunks, doc_title=None, user_id=None: [[0.0]],
                )
        assert count == 0
        assert "Aucun lien" in err

    def test_indexes_fetched_articles(self, app, db):
        from app.models.user_scraper import UserScraperArticle
        from app.services import user_scraper_service
        with app.app_context():
            u, s = self._setup(app, db)
            with patch.object(user_scraper_service, "_find_article_links",
                              return_value=["https://x.com/a", "https://x.com/b"]), \
                 patch.object(user_scraper_service, "_fetch_article",
                              return_value=("Title", "Body content. " * 30, None)):
                count, err = user_scraper_service._run_html_scrape(
                    scraper=s, user_id=u.id, existing_count=0,
                    max_articles=200, collection=MagicMock(),
                    db=db, UserScraperArticle=UserScraperArticle,
                    chunk_text=lambda t: [t],
                    _embed=lambda chunks, doc_title=None, user_id=None: [[0.0] for _ in chunks],
                )
        assert err is None
        assert count == 2


# ─── delete_scraper_from_index + scrape_all_for_user ─────────────────

class TestDeleteScraperFromIndex:
    def test_deletes_chroma_ids(self, app, db):
        from app.models.user import User
        from app.models.user_scraper import UserScraper, UserScraperArticle
        from app.services import user_scraper_service
        with app.app_context():
            u = User(
                email=f"del-{uuid.uuid4().hex[:6]}@example.com",
                is_confirmed=True, is_active=True,
                abonnement_type="premium_pro",
            )
            u.set_password("Del1234!")
            db.session.add(u)
            db.session.flush()
            s = UserScraper(
                user_id=u.id, name="X", url="https://x.com",
                scraper_type="rss", is_active=True,
            )
            db.session.add(s)
            db.session.flush()
            art = UserScraperArticle(
                scraper_id=s.id, user_id=u.id,
                url_hash="h", url="https://x.com/a",
                title="t", chunk_count=2,
            )
            db.session.add(art)
            db.session.commit()

            collection = MagicMock()
            with patch("app.services.rag_service._get_user_collection",
                       return_value=collection):
                user_scraper_service.delete_scraper_from_index(s.id, u.id)
        assert collection.delete.called
        ids = collection.delete.call_args.kwargs["ids"]
        assert len(ids) == 2

    def test_no_op_when_no_articles(self, app, db):
        from app.services import user_scraper_service
        with app.app_context():
            with patch("app.services.rag_service._get_user_collection") as mock_get:
                user_scraper_service.delete_scraper_from_index(99_999_999, 1)
        mock_get.assert_not_called()


class TestScrapeAllForUser:
    def test_iterates_user_scrapers(self, app, db):
        from app.models.user import User
        from app.models.user_scraper import UserScraper
        from app.services import user_scraper_service
        with app.app_context():
            u = User(
                email=f"all-{uuid.uuid4().hex[:6]}@example.com",
                is_confirmed=True, is_active=True,
                abonnement_type="premium_pro",
            )
            u.set_password("All1234!")
            db.session.add(u)
            db.session.flush()
            for i in range(3):
                db.session.add(UserScraper(
                    user_id=u.id, name=f"s{i}", url=f"https://x.com/{i}",
                    scraper_type="rss", is_active=True,
                ))
            db.session.commit()
            with patch.object(user_scraper_service, "scrape_source",
                              return_value=(2, None)) as mock_scrape:
                total = user_scraper_service.scrape_all_for_user(u.id)
        assert mock_scrape.call_count == 3
        assert total == 6


# ─── get_scraper_limit / get_article_limit ───────────────────────────

class TestGetScraperLimit:
    # OSS single-user build: source count is unlimited for every user.
    def test_unlimited_for_any_user(self):
        from app.services.user_scraper_service import get_scraper_limit
        assert get_scraper_limit(MagicMock(is_admin=True)) is None
        assert get_scraper_limit(MagicMock(is_admin=False)) is None


class TestGetArticleLimit:
    # OSS single-user build: articles-per-source is unlimited for every user.
    def test_unlimited_for_any_user(self):
        from app.services.user_scraper_service import get_article_limit
        assert get_article_limit(MagicMock(is_admin=True)) is None
        assert get_article_limit(MagicMock(is_admin=False)) is None
