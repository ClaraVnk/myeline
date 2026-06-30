"""Coverage for app/routes/user_scrapers.py — RSS/web scraper routes."""
import uuid
from unittest.mock import patch

import pytest

from tests.helpers import login


# ─────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────

@pytest.fixture
def pro_user(app, db):
    from app.models.user import User
    with app.app_context():
        u = User(
            email=f"pro-scrap-{uuid.uuid4().hex[:6]}@example.com",
            firstname="P", lastname="S",
            is_confirmed=True, is_active=True,
            abonnement_type="premium_pro",
            twofa_method="email",
        )
        u.set_password("ProUser1!")
        db.session.add(u)
        db.session.commit()
        user_id = u.id
    yield user_id
    with app.app_context():
        from app.models.user_scraper import UserScraper
        UserScraper.query.filter_by(user_id=user_id).delete()
        u = db.session.get(User, user_id)
        if u:
            db.session.delete(u)
        db.session.commit()


@pytest.fixture
def standard_user(app, db):
    from app.models.user import User
    with app.app_context():
        u = User(
            email=f"std-scrap-{uuid.uuid4().hex[:6]}@example.com",
            firstname="S", lastname="S",
            is_confirmed=True, is_active=True,
            abonnement_type="gratuit",
            twofa_method="email",
        )
        u.set_password("StdUser1!")
        db.session.add(u)
        db.session.commit()
        user_id = u.id
    yield user_id
    with app.app_context():
        u = db.session.get(User, user_id)
        if u:
            db.session.delete(u)
        db.session.commit()


def _login(client, app, db, user_id, password):
    from app.models.user import User
    with app.app_context():
        u = db.session.get(User, user_id)
        email = u.email
    return login(client, email, password)


# ─────────────────────────────────────────────────────────────────
# Auth gates
# ─────────────────────────────────────────────────────────────────

class TestAnonymousAccess:
    @pytest.mark.parametrize("path", [
        "/user/mes-sources",
        "/user/mes-sources/discover-feed?url=https://example.com",
        "/user/mes-sources/articles?scraper_id=1",
    ])
    def test_get_endpoints_redirect(self, client, path):
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (302, 401)

    @pytest.mark.parametrize("path", [
        "/user/mes-sources/add",
        "/user/mes-sources/delete",
        "/user/mes-sources/refresh",
        "/user/mes-sources/share",
        "/user/mes-sources/delete-article",
        "/user/mes-sources/update-since",
        "/user/mes-sources/update-frequency",
        "/user/mes-sources/unshare",
    ])
    def test_post_endpoints_redirect(self, client, path):
        r = client.post(path, follow_redirects=False)
        assert r.status_code in (302, 401)


# ─────────────────────────────────────────────────────────────────
# Scraper access — OSS single-user build: any confirmed account manages
# its sources without limit. Only unconfirmed / inactive accounts are
# rejected.
# ─────────────────────────────────────────────────────────────────

class TestScraperAccess:
    def test_free_user_sees_listing(self, client, app, db, standard_user):
        _login(client, app, db, standard_user, "StdUser1!")
        r = client.get("/user/mes-sources")
        assert r.status_code == 200

    def test_free_user_can_add_source(self, client, app, db, standard_user):
        _login(client, app, db, standard_user, "StdUser1!")
        with patch(
            "app.routes.user_scrapers.detect_source",
            return_value=("https://blog.example.com", "html"),
        ):
            with patch("threading.Thread"):
                r = client.post(
                    "/user/mes-sources/add",
                    data={"name": "blog", "url": "https://blog.example.com"},
                )
        assert r.status_code == 200
        assert r.get_json()["success"] is True

    def test_user_can_add_beyond_50_sources(self, client, app, db, standard_user):
        # OSS single-user build: sources are unlimited (get_scraper_limit
        # returns None) — the 51st source is accepted.
        from app.models.user_scraper import UserScraper

        with app.app_context():
            for i in range(50):
                db.session.add(UserScraper(
                    user_id=standard_user, name=f"src-{i}",
                    url=f"https://src{i}.example.com", scraper_type="html",
                ))
            db.session.commit()

        _login(client, app, db, standard_user, "StdUser1!")
        with patch(
            "app.routes.user_scrapers.detect_source",
            return_value=("https://overflow.com", "html"),
        ):
            with patch("threading.Thread"):
                r = client.post(
                    "/user/mes-sources/add",
                    data={"name": "overflow", "url": "https://overflow.com"},
                )
        assert r.status_code == 200
        assert r.get_json()["success"] is True


# ─────────────────────────────────────────────────────────────────
# scraper_list — Pro user listing
# ─────────────────────────────────────────────────────────────────

class TestScraperList:
    def test_pro_user_sees_listing(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.get("/user/mes-sources")
        assert r.status_code == 200


# ─────────────────────────────────────────────────────────────────
# discover_feed
# ─────────────────────────────────────────────────────────────────

class TestDiscoverFeed:
    def test_missing_url_returns_400(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.get("/user/mes-sources/discover-feed")
        assert r.status_code == 400
        assert r.get_json() == {"found": False}

    def test_url_with_feed_returns_feed_url(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        with patch(
            "app.routes.user_scrapers._try_rss",
            return_value="https://example.com/feed",
        ):
            r = client.get("/user/mes-sources/discover-feed?url=https://example.com")
        assert r.status_code == 200
        body = r.get_json()
        assert body["found"] is True
        assert body["feed_url"] == "https://example.com/feed"

    def test_url_without_feed_returns_not_found(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        with patch("app.routes.user_scrapers._try_rss", return_value=None):
            r = client.get("/user/mes-sources/discover-feed?url=https://example.com")
        assert r.status_code == 200
        assert r.get_json()["found"] is False

    def test_naked_url_gets_https_prefix(self, client, app, db, pro_user):
        """Bare 'example.com' is upgraded to 'https://example.com' before lookup."""
        _login(client, app, db, pro_user, "ProUser1!")
        with patch(
            "app.routes.user_scrapers._try_rss", return_value=None,
        ) as rss:
            client.get("/user/mes-sources/discover-feed?url=example.com")
        assert rss.called
        called_url = rss.call_args.args[0]
        assert called_url.startswith("https://")


# ─────────────────────────────────────────────────────────────────
# add_scraper
# ─────────────────────────────────────────────────────────────────

class TestAddScraper:
    def test_missing_required_fields_returns_400(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.post(
            "/user/mes-sources/add",
            data={"name": "", "url": ""},
        )
        assert r.status_code == 400
        assert r.get_json()["success"] is False

    def test_invalid_date_format_returns_400(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.post(
            "/user/mes-sources/add",
            data={
                "name": "blog", "url": "https://example.com",
                "scrape_since": "not-a-date",
            },
        )
        assert r.status_code == 400
        assert "date" in r.get_json()["message"].lower()

    def test_unreachable_url_returns_400(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        with patch(
            "app.routes.user_scrapers.detect_source",
            side_effect=ValueError("URL not reachable"),
        ):
            r = client.post(
                "/user/mes-sources/add",
                data={"name": "blog", "url": "https://broken.example"},
            )
        assert r.status_code == 400
        assert "reachable" in r.get_json()["message"].lower() or \
               "not" in r.get_json()["message"].lower()

    def test_pro_user_has_unlimited_sources(self, client, app, db, pro_user):
        # Pivot Free/Pro : get_scraper_limit renvoie None (illimité) pour un
        # abonné Pro individuel. Même avec 50 sources déjà présentes, l'ajout
        # réussit — il n'y a plus de cap à 50 pour Pro.
        from app.models.user_scraper import UserScraper

        with app.app_context():
            for i in range(50):
                s = UserScraper(
                    user_id=pro_user, name=f"src-{i}",
                    url=f"https://src{i}.example.com",
                    scraper_type="html",
                )
                db.session.add(s)
            db.session.commit()

        _login(client, app, db, pro_user, "ProUser1!")
        with patch(
            "app.routes.user_scrapers.detect_source",
            return_value=("https://overflow.com", "html"),
        ):
            with patch("threading.Thread"):  # avoid background scrape
                r = client.post(
                    "/user/mes-sources/add",
                    data={"name": "overflow", "url": "https://overflow.com"},
                )
        assert r.status_code == 200
        assert r.get_json()["success"] is True

    def test_duplicate_url_returns_400(self, client, app, db, pro_user):
        from app.models.user_scraper import UserScraper

        with app.app_context():
            s = UserScraper(
                user_id=pro_user, name="dup",
                url="https://duplicate.example.com",
                scraper_type="html",
            )
            db.session.add(s)
            db.session.commit()

        _login(client, app, db, pro_user, "ProUser1!")
        with patch(
            "app.routes.user_scrapers.detect_source",
            return_value=("https://duplicate.example.com", "html"),
        ):
            r = client.post(
                "/user/mes-sources/add",
                data={"name": "another", "url": "https://duplicate.example.com"},
            )
        assert r.status_code == 400
        assert "déjà" in r.get_json()["message"]

    def test_success_creates_scraper(self, client, app, db, pro_user):
        from app.models.user_scraper import UserScraper

        _login(client, app, db, pro_user, "ProUser1!")
        with patch(
            "app.routes.user_scrapers.detect_source",
            return_value=("https://newblog.example.com/feed", "rss"),
        ):
            with patch("threading.Thread"):  # avoid background scrape
                r = client.post(
                    "/user/mes-sources/add",
                    data={"name": "newblog", "url": "https://newblog.example.com"},
                )
        assert r.status_code == 200
        body = r.get_json()
        assert body["success"] is True
        assert body["scraper_type"] == "rss"

        with app.app_context():
            assert UserScraper.query.filter_by(
                user_id=pro_user, url="https://newblog.example.com/feed"
            ).first() is not None


# ─────────────────────────────────────────────────────────────────
# delete_scraper
# ─────────────────────────────────────────────────────────────────

class TestDeleteScraper:
    def test_missing_id_returns_400(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.post("/user/mes-sources/delete", json={})
        assert r.status_code == 400

    def test_unknown_scraper_returns_404(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.post("/user/mes-sources/delete", json={"scraper_id": 99999})
        assert r.status_code == 404

    def test_other_users_scraper_returns_404(self, client, app, db, pro_user):
        from app.models.user_scraper import UserScraper

        with app.app_context():
            s = UserScraper(
                user_id=pro_user + 9999, name="alien",  # different user
                url="https://alien.example.com", scraper_type="html",
            )
            db.session.add(s)
            db.session.commit()
            sid = s.id

        _login(client, app, db, pro_user, "ProUser1!")
        with patch("app.routes.user_scrapers.delete_scraper_from_index"):
            r = client.post("/user/mes-sources/delete", json={"scraper_id": sid})
        assert r.status_code == 404

        with app.app_context():
            s = db.session.get(UserScraper, sid)
            if s:
                db.session.delete(s)
                db.session.commit()

    def test_success_deletes_scraper(self, client, app, db, pro_user):
        from app.models.user_scraper import UserScraper

        with app.app_context():
            s = UserScraper(
                user_id=pro_user, name="todelete",
                url="https://todelete.example.com", scraper_type="html",
            )
            db.session.add(s)
            db.session.commit()
            sid = s.id

        _login(client, app, db, pro_user, "ProUser1!")
        with patch("app.routes.user_scrapers.delete_scraper_from_index"):
            r = client.post("/user/mes-sources/delete", json={"scraper_id": sid})
        assert r.status_code == 200
        assert r.get_json()["success"] is True

        with app.app_context():
            assert db.session.get(UserScraper, sid) is None


# ─────────────────────────────────────────────────────────────────
# refresh_scraper
# ─────────────────────────────────────────────────────────────────

class TestRefreshScraper:
    def test_missing_id_returns_400(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.post("/user/mes-sources/refresh", json={})
        assert r.status_code == 400

    def test_unknown_scraper_returns_404(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.post("/user/mes-sources/refresh", json={"scraper_id": 99999})
        assert r.status_code == 404

    def test_dispatches_background_thread(self, client, app, db, pro_user):
        from app.models.user_scraper import UserScraper

        with app.app_context():
            s = UserScraper(
                user_id=pro_user, name="refreshme",
                url="https://refresh.example.com", scraper_type="html",
            )
            db.session.add(s)
            db.session.commit()
            sid = s.id

        _login(client, app, db, pro_user, "ProUser1!")
        with patch("threading.Thread") as Thread:
            r = client.post("/user/mes-sources/refresh", json={"scraper_id": sid})
        assert r.status_code == 200
        assert r.get_json()["success"] is True
        Thread.assert_called_once()


# ─────────────────────────────────────────────────────────────────
# share_scraper
# ─────────────────────────────────────────────────────────────────

class TestShareScraper:
    def test_unknown_scraper_returns_404(self, client, app, db, pro_user):
        # User-to-user sharing: sharing a source the caller does not own
        # returns 404.
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.post(
            "/user/mes-sources/share",
            json={"scraper_id": 999999, "email": "x@example.com"},
        )
        assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────
# scraper_articles
# ─────────────────────────────────────────────────────────────────

class TestScraperArticles:
    def test_missing_id_returns_400(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.get("/user/mes-sources/articles")
        assert r.status_code == 400

    def test_unknown_scraper_returns_404(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.get("/user/mes-sources/articles?scraper_id=99999")
        assert r.status_code == 404

    def test_success_returns_articles(self, client, app, db, pro_user):
        from datetime import datetime, timezone

        from app.models.user_scraper import UserScraper, UserScraperArticle

        with app.app_context():
            s = UserScraper(
                user_id=pro_user, name="src",
                url="https://src.example.com", scraper_type="html",
            )
            db.session.add(s)
            db.session.flush()
            a = UserScraperArticle(
                scraper_id=s.id, user_id=pro_user,
                title="Article 1", url="https://src.example.com/a1",
                url_hash="h" * 64,
                content="body", chunk_count=1,
                created_at=datetime.now(timezone.utc),
            )
            db.session.add(a)
            db.session.commit()
            sid = s.id

        _login(client, app, db, pro_user, "ProUser1!")
        r = client.get(f"/user/mes-sources/articles?scraper_id={sid}")
        assert r.status_code == 200
        body = r.get_json()
        assert body["success"] is True
        assert len(body["articles"]) == 1
        assert body["articles"][0]["title"] == "Article 1"


# ─────────────────────────────────────────────────────────────────
# update_scraper_frequency
# ─────────────────────────────────────────────────────────────────

class TestUpdateScraperFrequency:
    def test_unknown_scraper_returns_404(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.post(
            "/user/mes-sources/update-frequency",
            json={"scraper_id": 99999, "frequency": "daily"},
        )
        assert r.status_code == 404

    def test_invalid_frequency_returns_400(self, client, app, db, pro_user):
        from app.models.user_scraper import UserScraper

        with app.app_context():
            s = UserScraper(
                user_id=pro_user, name="src",
                url="https://src.example.com", scraper_type="html",
            )
            db.session.add(s)
            db.session.commit()
            sid = s.id

        _login(client, app, db, pro_user, "ProUser1!")
        r = client.post(
            "/user/mes-sources/update-frequency",
            json={"scraper_id": sid, "frequency": "yearly"},
        )
        assert r.status_code == 400

    def test_valid_frequency_persists(self, client, app, db, pro_user):
        from app.models.user_scraper import UserScraper

        with app.app_context():
            s = UserScraper(
                user_id=pro_user, name="src",
                url="https://src.example.com", scraper_type="html",
                scrape_frequency="daily",
            )
            db.session.add(s)
            db.session.commit()
            sid = s.id

        _login(client, app, db, pro_user, "ProUser1!")
        r = client.post(
            "/user/mes-sources/update-frequency",
            json={"scraper_id": sid, "frequency": "weekly"},
        )
        assert r.status_code == 200

        with app.app_context():
            s_after = db.session.get(UserScraper, sid)
            assert s_after.scrape_frequency == "weekly"


# ─────────────────────────────────────────────────────────────────
# update_scraper_since
# ─────────────────────────────────────────────────────────────────

class TestUpdateScraperSince:
    def test_unknown_scraper_returns_404(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.post(
            "/user/mes-sources/update-since",
            json={"scraper_id": 99999, "scrape_since": "2024-01-01"},
        )
        assert r.status_code == 404

    def test_invalid_date_returns_400(self, client, app, db, pro_user):
        from app.models.user_scraper import UserScraper

        with app.app_context():
            s = UserScraper(
                user_id=pro_user, name="src",
                url="https://src.example.com", scraper_type="html",
            )
            db.session.add(s)
            db.session.commit()
            sid = s.id

        _login(client, app, db, pro_user, "ProUser1!")
        r = client.post(
            "/user/mes-sources/update-since",
            json={"scraper_id": sid, "scrape_since": "not-a-date"},
        )
        assert r.status_code == 400


# ─────────────────────────────────────────────────────────────────
# delete_scraper_article
# ─────────────────────────────────────────────────────────────────

class TestDeleteScraperArticle:
    def test_missing_id_returns_400(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.post("/user/mes-sources/delete-article", json={})
        assert r.status_code == 400

    def test_unknown_article_returns_404(self, client, app, db, pro_user):
        _login(client, app, db, pro_user, "ProUser1!")
        r = client.post(
            "/user/mes-sources/delete-article",
            json={"article_id": 99999},
        )
        assert r.status_code == 404
