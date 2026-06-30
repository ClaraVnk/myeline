"""Coverage for app/routes/user_scrapers.py — refresh / share / articles
/ delete-article / update-since / update-frequency / unshare endpoints.
Existing test_user_scrapers_routes.py covers the list/discover/add paths.
"""
import uuid

import pytest

from tests.helpers import login


# ─── Fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def pro_user(app, db):
    from app.models.user import User
    with app.app_context():
        u = User(
            email=f"sc-pro-{uuid.uuid4().hex[:6]}@example.com",
            firstname="P", lastname="C",
            is_confirmed=True, is_active=True,
            abonnement_type="premium_pro",
            twofa_method="email",
        )
        u.set_password("ProSc1234!")
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
def scraper(app, db, pro_user):
    from app.models.user_scraper import UserScraper
    with app.app_context():
        s = UserScraper(
            user_id=pro_user, name="T", url="https://example.com/feed",
            scraper_type="rss", is_active=True,
        )
        db.session.add(s)
        db.session.commit()
        return s.id


def _email(app, db, user_id):
    from app.models.user import User
    with app.app_context():
        return db.session.get(User, user_id).email


# ─── refresh ─────────────────────────────────────────────────────────

class TestRefresh:
    def test_missing_id_returns_400(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProSc1234!")
        r = client.post("/user/mes-sources/refresh", json={})
        assert r.status_code == 400

    def test_unknown_scraper_returns_404(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProSc1234!")
        r = client.post("/user/mes-sources/refresh",
                        json={"scraper_id": 999_999})
        assert r.status_code == 404

    def test_dispatches_async_refresh(self, client, app, db, pro_user, scraper):
        login(client, _email(app, db, pro_user), "ProSc1234!")
        r = client.post("/user/mes-sources/refresh",
                        json={"scraper_id": scraper})
        assert r.status_code == 200
        assert r.get_json()["success"] is True


# ─── share ───────────────────────────────────────────────────────────

class TestShare:
    def test_unknown_target_email_404(self, client, app, db, pro_user, scraper):
        # User-to-user sharing: sharing an owned source with an email that
        # matches no account returns 404.
        login(client, _email(app, db, pro_user), "ProSc1234!")
        r = client.post("/user/mes-sources/share",
                        json={"scraper_id": scraper, "email": "x@y.com"})
        assert r.status_code == 404


# ─── articles ────────────────────────────────────────────────────────

class TestScraperArticles:
    def test_returns_articles_for_owner(self, client, app, db, pro_user, scraper):
        from app.models.user_scraper import UserScraperArticle
        with app.app_context():
            db.session.add(UserScraperArticle(
                scraper_id=scraper, user_id=pro_user,
                url_hash="h1", url="https://x.com/1",
                title="A1", chunk_count=2,
            ))
            db.session.commit()
        login(client, _email(app, db, pro_user), "ProSc1234!")
        r = client.get(f"/user/mes-sources/articles?scraper_id={scraper}")
        assert r.status_code == 200
        data = r.get_json()
        assert any(a["title"] == "A1" for a in data["articles"])

    def test_missing_id_400(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProSc1234!")
        r = client.get("/user/mes-sources/articles")
        assert r.status_code == 400

    def test_unknown_scraper_404(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProSc1234!")
        r = client.get("/user/mes-sources/articles?scraper_id=99999")
        assert r.status_code == 404


# ─── delete-article ──────────────────────────────────────────────────

class TestDeleteScraperArticle:
    def test_missing_id_400(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProSc1234!")
        r = client.post("/user/mes-sources/delete-article", json={})
        assert r.status_code == 400

    def test_unknown_id_404(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProSc1234!")
        r = client.post("/user/mes-sources/delete-article",
                        json={"article_id": 999_999})
        assert r.status_code == 404

    def test_deletes_article(self, client, app, db, pro_user, scraper):
        from unittest.mock import patch

        from app.models.user_scraper import UserScraperArticle
        with app.app_context():
            art = UserScraperArticle(
                scraper_id=scraper, user_id=pro_user,
                url_hash="h2", url="https://x.com/2",
                title="To delete", chunk_count=2,
            )
            db.session.add(art)
            db.session.commit()
            art_id = art.id
        login(client, _email(app, db, pro_user), "ProSc1234!")
        with patch("app.services.rag_service._get_user_collection"):
            r = client.post("/user/mes-sources/delete-article",
                            json={"article_id": art_id})
        assert r.status_code == 200


# ─── update-since ────────────────────────────────────────────────────

class TestUpdateSince:
    def test_unknown_scraper_404(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProSc1234!")
        r = client.post("/user/mes-sources/update-since",
                        json={"scraper_id": 99999, "scrape_since": "2026-01-01"})
        assert r.status_code == 404

    def test_invalid_date_format(self, client, app, db, pro_user, scraper):
        login(client, _email(app, db, pro_user), "ProSc1234!")
        r = client.post("/user/mes-sources/update-since",
                        json={"scraper_id": scraper, "scrape_since": "01/01/2026"})
        assert r.status_code == 400

    def test_clears_when_empty(self, client, app, db, pro_user, scraper):
        from app.models.user_scraper import UserScraper
        login(client, _email(app, db, pro_user), "ProSc1234!")
        r = client.post("/user/mes-sources/update-since",
                        json={"scraper_id": scraper, "scrape_since": ""})
        assert r.status_code == 200
        with app.app_context():
            s = db.session.get(UserScraper, scraper)
            assert s.scrape_since is None

    def test_sets_valid_date(self, client, app, db, pro_user, scraper):
        from app.models.user_scraper import UserScraper
        login(client, _email(app, db, pro_user), "ProSc1234!")
        r = client.post("/user/mes-sources/update-since",
                        json={"scraper_id": scraper, "scrape_since": "2026-03-15"})
        assert r.status_code == 200
        with app.app_context():
            s = db.session.get(UserScraper, scraper)
        assert s.scrape_since is not None


# ─── update-frequency ────────────────────────────────────────────────

class TestUpdateFrequency:
    def test_invalid_frequency_400(self, client, app, db, pro_user, scraper):
        login(client, _email(app, db, pro_user), "ProSc1234!")
        r = client.post("/user/mes-sources/update-frequency",
                        json={"scraper_id": scraper, "frequency": "hourly"})
        assert r.status_code == 400

    def test_unknown_scraper_404(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProSc1234!")
        r = client.post("/user/mes-sources/update-frequency",
                        json={"scraper_id": 999_999, "frequency": "weekly"})
        assert r.status_code == 404

    def test_valid_frequency_persists(self, client, app, db, pro_user, scraper):
        from app.models.user_scraper import UserScraper
        login(client, _email(app, db, pro_user), "ProSc1234!")
        r = client.post("/user/mes-sources/update-frequency",
                        json={"scraper_id": scraper, "frequency": "weekly"})
        assert r.status_code == 200
        with app.app_context():
            s = db.session.get(UserScraper, scraper)
        assert s.scrape_frequency == "weekly"


# ─── unshare ─────────────────────────────────────────────────────────

class TestUnshare:
    def test_missing_share_id_400(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProSc1234!")
        r = client.post("/user/mes-sources/unshare", json={})
        assert r.status_code == 400

    def test_unknown_share_404(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ProSc1234!")
        r = client.post("/user/mes-sources/unshare",
                        json={"share_id": 999_999})
        assert r.status_code == 404
