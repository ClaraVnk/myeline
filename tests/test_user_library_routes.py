"""Tests for user library routes (app/routes/user_library.py).

Current routes:
  - GET  /user/ma-bibliotheque                              (listing)
  - GET  /user/ma-bibliotheque/preview/<doc_type>/<doc_id>  (JSON preview)
  - POST /user/ma-bibliotheque/cloud-doc/remove-index       (remove cloud doc from index)
"""
import json
import uuid
from unittest.mock import MagicMock, patch

import pytest

from tests.helpers import login


# ── Helpers ───────────────────────────────────────────────────────────────────


def _login_subscriber(client):
    login(client, "subscriber@example.com", "Alice1234!")


def _login_confirmed(client):
    login(client, "alice@example.com", "Alice1234!")


def _make_cloud_connection(db, user):
    from app.models.cloud_connection import CloudConnection

    conn = CloudConnection(
        user_id=user.id,
        provider="gdrive",
        access_token_enc=b"fake",
    )
    db.session.add(conn)
    db.session.flush()
    return conn


def _make_cloud_doc(db, conn, name="test.pdf"):
    from app.models.cloud_connection import CloudDocument

    doc = CloudDocument(
        connection_id=conn.id,
        user_id=conn.user_id,
        remote_id=f"remote-{uuid.uuid4().hex[:8]}",
        name=name,
        mime_type="application/pdf",
        file_size=1024,
        chunk_count=0,
    )
    db.session.add(doc)
    db.session.flush()
    return doc


def _make_scraper_article(db, user, title="Article Test"):
    import hashlib

    from app.models.user_scraper import UserScraper, UserScraperArticle

    scraper = UserScraper(
        user_id=user.id,
        name="Test Scraper",
        url=f"https://example.com/feed-{uuid.uuid4().hex[:6]}.xml",
        scraper_type="rss",
    )
    db.session.add(scraper)
    db.session.flush()

    article_url = f"https://example.com/article-{uuid.uuid4().hex[:8]}"
    article = UserScraperArticle(
        scraper_id=scraper.id,
        user_id=user.id,
        title=title,
        url=article_url,
        url_hash=hashlib.sha256(article_url.encode()).hexdigest(),
        content="Content of the article",
        chunk_count=2,
    )
    db.session.add(article)
    db.session.flush()
    return scraper, article


# ── Auth guards ───────────────────────────────────────────────────────────────


def test_library_requires_login(client):
    r = client.get("/user/ma-bibliotheque", follow_redirects=False)
    assert r.status_code in (301, 302)


def test_preview_requires_login(client):
    r = client.get("/user/ma-bibliotheque/preview/cloud/1", follow_redirects=False)
    assert r.status_code in (301, 302)


def test_remove_index_requires_login(client):
    r = client.post(
        "/user/ma-bibliotheque/cloud-doc/remove-index",
        data=json.dumps({"doc_id": 1}),
        content_type="application/json",
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)


# ── Subscriber guard ──────────────────────────────────────────────────────────


def test_library_free_user_reaches_library(client, confirmed_user):
    """Pivot Free/Pro : confirmed_user (gratuit, confirmé & actif) est
    désormais subscriber pour l'accès de base. Le gate _require_subscriber
    le laisse passer — la bibliothèque répond 200."""
    _login_confirmed(client)
    r = client.get("/user/ma-bibliotheque", follow_redirects=False)
    assert r.status_code == 200
    client.get("/logout")


# ── GET /ma-bibliotheque ──────────────────────────────────────────────────────


def test_library_get_empty(client, subscriber_user):
    _login_subscriber(client)
    r = client.get("/user/ma-bibliotheque")
    assert r.status_code == 200
    client.get("/logout")


def test_library_get_with_documents(client, subscriber_user, db):
    _login_subscriber(client)
    conn = _make_cloud_connection(db, subscriber_user)
    doc = _make_cloud_doc(db, conn, "rapport.pdf")
    db.session.commit()
    try:
        r = client.get("/user/ma-bibliotheque")
        assert r.status_code == 200
    finally:
        db.session.delete(doc)
        db.session.delete(conn)
        db.session.commit()
    client.get("/logout")


def test_library_get_pagination(client, subscriber_user):
    _login_subscriber(client)
    r = client.get("/user/ma-bibliotheque?page=1&per_page=10")
    assert r.status_code == 200
    client.get("/logout")


def test_library_get_invalid_per_page_falls_back(client, subscriber_user):
    """per_page not in (10, 50, 100) → falls back to 10."""
    _login_subscriber(client)
    r = client.get("/user/ma-bibliotheque?per_page=999")
    assert r.status_code == 200
    client.get("/logout")


def test_library_get_search_query(client, subscriber_user):
    _login_subscriber(client)
    r = client.get("/user/ma-bibliotheque?q=rapport")
    assert r.status_code == 200
    client.get("/logout")


def test_library_get_tag_filter(client, subscriber_user):
    _login_subscriber(client)
    r = client.get("/user/ma-bibliotheque?tag=science")
    assert r.status_code == 200
    client.get("/logout")


# ── GET /ma-bibliotheque/preview ──────────────────────────────────────────────


def test_preview_invalid_doc_type_returns_400(client, subscriber_user):
    _login_subscriber(client)
    r = client.get("/user/ma-bibliotheque/preview/badtype/1")
    assert r.status_code == 400
    assert "error" in r.get_json()
    client.get("/logout")


def test_preview_cloud_doc_not_found_returns_404(client, subscriber_user):
    _login_subscriber(client)
    r = client.get("/user/ma-bibliotheque/preview/cloud/999999")
    assert r.status_code == 404
    client.get("/logout")


def test_preview_cloud_doc_owned_by_other_user_returns_404(client, subscriber_user, confirmed_user, db):
    """A cloud doc belonging to another user's connection must not be accessible."""
    conn = _make_cloud_connection(db, confirmed_user)
    doc = _make_cloud_doc(db, conn, "other_user.pdf")
    db.session.commit()

    _login_subscriber(client)
    r = client.get(f"/user/ma-bibliotheque/preview/cloud/{doc.id}")
    assert r.status_code == 404

    db.session.delete(doc)
    db.session.delete(conn)
    db.session.commit()
    client.get("/logout")


def test_preview_cloud_doc_success(client, subscriber_user, db):
    """Preview of own cloud doc returns JSON with expected fields."""
    conn = _make_cloud_connection(db, subscriber_user)
    doc = _make_cloud_doc(db, conn, "my_report.pdf")
    doc.chunk_count = 0
    db.session.commit()

    _login_subscriber(client)
    # chunk_count=0 → no ChromaDB call, no need to patch
    r = client.get(f"/user/ma-bibliotheque/preview/cloud/{doc.id}")

    assert r.status_code == 200
    data = r.get_json()
    assert data["type"] == "cloud"
    assert data["name"] == "my_report.pdf"
    assert "chunk_count" in data

    db.session.delete(doc)
    db.session.delete(conn)
    db.session.commit()
    client.get("/logout")


def test_preview_scraper_article_not_found_returns_404(client, subscriber_user):
    _login_subscriber(client)
    r = client.get("/user/ma-bibliotheque/preview/scraper/999999")
    assert r.status_code == 404
    client.get("/logout")


def test_preview_scraper_article_owned_by_other_returns_404(client, subscriber_user, confirmed_user, db):
    scraper, article = _make_scraper_article(db, confirmed_user, "Secret article")
    db.session.commit()

    _login_subscriber(client)
    r = client.get(f"/user/ma-bibliotheque/preview/scraper/{article.id}")
    assert r.status_code == 404

    db.session.delete(article)
    db.session.delete(scraper)
    db.session.commit()
    client.get("/logout")


def test_preview_scraper_article_success(client, subscriber_user, db):
    scraper, article = _make_scraper_article(db, subscriber_user, "Mon article RSS")
    db.session.commit()

    _login_subscriber(client)
    r = client.get(f"/user/ma-bibliotheque/preview/scraper/{article.id}")
    assert r.status_code == 200
    data = r.get_json()
    assert data["type"] == "scraper"
    assert data["title"] == "Mon article RSS"
    assert "chunk_count" in data

    db.session.delete(article)
    db.session.delete(scraper)
    db.session.commit()
    client.get("/logout")


# ── POST /ma-bibliotheque/cloud-doc/remove-index ──────────────────────────────


def test_remove_index_missing_doc_id_returns_400(client, subscriber_user):
    _login_subscriber(client)
    r = client.post(
        "/user/ma-bibliotheque/cloud-doc/remove-index",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert r.status_code == 400
    assert r.get_json()["success"] is False
    client.get("/logout")


def test_remove_index_doc_not_found_returns_404(client, subscriber_user):
    _login_subscriber(client)
    r = client.post(
        "/user/ma-bibliotheque/cloud-doc/remove-index",
        data=json.dumps({"doc_id": 999999}),
        content_type="application/json",
    )
    assert r.status_code == 404
    client.get("/logout")


def test_remove_index_doc_owned_by_other_returns_404(client, subscriber_user, confirmed_user, db):
    conn = _make_cloud_connection(db, confirmed_user)
    doc = _make_cloud_doc(db, conn, "other.pdf")
    db.session.commit()

    _login_subscriber(client)
    r = client.post(
        "/user/ma-bibliotheque/cloud-doc/remove-index",
        data=json.dumps({"doc_id": doc.id}),
        content_type="application/json",
    )
    assert r.status_code == 404

    db.session.delete(doc)
    db.session.delete(conn)
    db.session.commit()
    client.get("/logout")


def test_remove_index_success_resets_chunk_count(client, subscriber_user, db):
    conn = _make_cloud_connection(db, subscriber_user)
    doc = _make_cloud_doc(db, conn, "indexed.pdf")
    doc.chunk_count = 5
    db.session.commit()

    _login_subscriber(client)
    with patch("app.services.rag_service.remove_cloud_document_from_index"):
        r = client.post(
            "/user/ma-bibliotheque/cloud-doc/remove-index",
            data=json.dumps({"doc_id": doc.id}),
            content_type="application/json",
        )

    assert r.status_code == 200
    assert r.get_json()["success"] is True

    # The route deletes the CloudDocument so the next sync treats the file as new
    # and re-indexes it from scratch (full sync, not delta).
    from app.models.cloud_connection import CloudDocument
    db.session.expire_all()
    assert db.session.get(CloudDocument, doc.id) is None
    # sync_cursor is also reset on the connection to force a full re-sync.
    db.session.refresh(conn)
    assert conn.sync_cursor is None

    db.session.delete(conn)
    db.session.commit()
    client.get("/logout")
