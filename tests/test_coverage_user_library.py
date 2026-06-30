"""Coverage push — user_library routes (preview, remove-index, chat).

Tested:
- /user/ma-bibliotheque (renders; anonymous redirected; non-subscriber redirect)
- /user/ma-bibliotheque/preview/<type>/<id> (cloud / scraper / 404 / invalid)
- /user/ma-bibliotheque/cloud-doc/remove-index POST (happy + 404 + missing param)
- /user/ma-bibliotheque/chat/<type>/<id> (Pro gating + per-type lookups)
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest


def _mk_pro_user(db, app):
    """Pro user (subscriber + Pro+) usable across tests."""
    from app.models.user import User
    with app.app_context():
        u = User(
            email=f"pro-{uuid.uuid4().hex[:8]}@test.local",
            is_confirmed=True, is_active=True,
            twofa_method="totp", totp_secret="JBSWY3DPEHPK3PXP",
            abonnement_type="premium_pro",
        )
        u.set_password("x")
        db.session.add(u)
        db.session.commit()
        return u.id


def _mk_non_subscriber_user(db, app):
    """A user who fails the base is_subscriber gate.

    Pivot Free/Pro/Enterprise: a confirmed + active Free (gratuit) account is
    now a base subscriber. To exercise the _require_subscriber redirect we
    need an account that is NOT (active AND confirmed) — here an unconfirmed
    Free user (is_subscriber is False)."""
    from app.models.user import User
    with app.app_context():
        u = User(
            email=f"std-{uuid.uuid4().hex[:8]}@test.local",
            is_confirmed=False, is_active=True,
            twofa_method="totp", totp_secret="JBSWY3DPEHPK3PXP",
            abonnement_type="gratuit",
        )
        u.set_password("x")
        db.session.add(u)
        db.session.commit()
        return u.id


def _mk_free_user(db, app):
    """A confirmed + active 'gratuit' account.

    OSS single-user build: tier gating is removed, so this account has the
    same full access as any other confirmed user."""
    from app.models.user import User
    with app.app_context():
        u = User(
            email=f"free-{uuid.uuid4().hex[:8]}@test.local",
            is_confirmed=True, is_active=True,
            twofa_method="totp", totp_secret="JBSWY3DPEHPK3PXP",
            abonnement_type="gratuit",
        )
        u.set_password("x")
        db.session.add(u)
        db.session.commit()
        return u.id


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


# ────────────────────────────────────────────────────────────────────
# personal_library — list view
# ────────────────────────────────────────────────────────────────────

class TestPersonalLibrary:
    def test_anonymous_is_redirected(self, client):
        r = client.get("/user/ma-bibliotheque", follow_redirects=False)
        assert r.status_code in (301, 302, 401, 403)

    def test_non_subscriber_is_redirected(self, client, db, app):
        uid = _mk_non_subscriber_user(db, app)
        _login(client, uid)
        r = client.get("/user/ma-bibliotheque", follow_redirects=False)
        assert r.status_code in (301, 302)
        # Subscriber gate sends them off the route — the only thing that
        # matters is they don't render the library page.
        assert r.location is not None

    def test_pro_user_renders_empty_library(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.get("/user/ma-bibliotheque")
        assert r.status_code == 200

    def test_free_user_renders_library(self, client, db, app):
        uid = _mk_free_user(db, app)
        _login(client, uid)
        r = client.get("/user/ma-bibliotheque")
        assert r.status_code == 200

    def test_renders_with_filters(self, client, db, app):
        """The query-string filters all execute their respective branches."""
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.get(
            "/user/ma-bibliotheque?q=foo&provider=gdrive&tag=bar"
            "&source=cloud&per_page=50&page=1"
        )
        assert r.status_code == 200

    def test_renders_with_scraper_source_filter(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.get("/user/ma-bibliotheque?source=scraper")
        assert r.status_code == 200

    def test_invalid_per_page_falls_back_to_default(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.get("/user/ma-bibliotheque?per_page=99999")
        assert r.status_code == 200


# ────────────────────────────────────────────────────────────────────
# doc_preview
# ────────────────────────────────────────────────────────────────────

class TestDocPreview:
    def test_cloud_404_for_unknown_doc(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.get("/user/ma-bibliotheque/preview/cloud/9999")
        assert r.status_code == 404

    def test_scraper_404_for_unknown_doc(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.get("/user/ma-bibliotheque/preview/scraper/9999")
        assert r.status_code == 404

    def test_invalid_type_returns_400(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.get("/user/ma-bibliotheque/preview/garbage/1")
        assert r.status_code == 400

    def test_cloud_preview_returns_metadata(self, client, db, app):
        from app.models.cloud_connection import CloudConnection, CloudDocument
        uid = _mk_pro_user(db, app)
        with app.app_context():
            conn = CloudConnection(
                user_id=uid, provider="gdrive",
                access_token_enc="enc", folder_name="Drive",
            )
            db.session.add(conn)
            db.session.flush()
            doc = CloudDocument(
                connection_id=conn.id, user_id=uid, remote_id="f1",
                name="Hello.pdf", mime_type="application/pdf",
                file_size=123, chunk_count=0,
            )
            db.session.add(doc)
            db.session.commit()
            doc_id = doc.id
        _login(client, uid)
        r = client.get(f"/user/ma-bibliotheque/preview/cloud/{doc_id}")
        assert r.status_code == 200
        assert r.is_json
        body = r.get_json()
        assert body["name"] == "Hello.pdf"
        assert body["mime_type"] == "application/pdf"

    def test_scraper_preview_returns_content(self, client, db, app):
        from app.models.user_scraper import UserScraper, UserScraperArticle
        uid = _mk_pro_user(db, app)
        with app.app_context():
            sc = UserScraper(
                user_id=uid, name="my-rss", url="http://ex.com/rss",
                scraper_type="rss", is_active=True,
            )
            db.session.add(sc)
            db.session.flush()
            import hashlib
            url = "http://ex.com/article"
            art = UserScraperArticle(
                scraper_id=sc.id, user_id=uid,
                url_hash=hashlib.sha256(url.encode()).hexdigest(),
                title="My Article",
                url=url,
                content="The body of the article.",
                chunk_count=0,
            )
            db.session.add(art)
            db.session.commit()
            art_id = art.id
        _login(client, uid)
        r = client.get(f"/user/ma-bibliotheque/preview/scraper/{art_id}")
        assert r.status_code == 200
        body = r.get_json()
        assert body["title"] == "My Article"
        assert "body of the article" in body["content"]


# ────────────────────────────────────────────────────────────────────
# remove_cloud_doc_from_index
# ────────────────────────────────────────────────────────────────────

class TestRemoveCloudDocFromIndex:
    def test_missing_doc_id_returns_400(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.post("/user/ma-bibliotheque/cloud-doc/remove-index",
                        json={})
        assert r.status_code == 400

    def test_unknown_doc_returns_404(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.post("/user/ma-bibliotheque/cloud-doc/remove-index",
                        json={"doc_id": 9999})
        assert r.status_code == 404

    def test_removes_doc_owned_by_user(self, client, db, app):
        from app.models.cloud_connection import CloudConnection, CloudDocument
        uid = _mk_pro_user(db, app)
        with app.app_context():
            conn = CloudConnection(
                user_id=uid, provider="gdrive",
                access_token_enc="enc", folder_name="Drive",
                sync_cursor="some-cursor",
            )
            db.session.add(conn)
            db.session.flush()
            doc = CloudDocument(
                connection_id=conn.id, user_id=uid, remote_id="f2",
                name="Bye.pdf", chunk_count=3,
            )
            db.session.add(doc)
            db.session.commit()
            doc_id, conn_id = doc.id, conn.id
        _login(client, uid)
        with patch("app.services.rag_service.remove_cloud_document_from_index") as mock_rm:
            r = client.post("/user/ma-bibliotheque/cloud-doc/remove-index",
                            json={"doc_id": doc_id})
        assert r.status_code == 200
        assert r.get_json() == {"success": True}
        mock_rm.assert_called_once_with(uid, doc_id, 3)
        with app.app_context():
            assert db.session.get(CloudDocument, doc_id) is None
            # sync_cursor should have been reset.
            assert db.session.get(CloudConnection, conn_id).sync_cursor is None

    def test_other_users_doc_is_404(self, client, db, app):
        from app.models.cloud_connection import CloudConnection, CloudDocument
        owner = _mk_pro_user(db, app)
        intruder = _mk_pro_user(db, app)
        with app.app_context():
            conn = CloudConnection(
                user_id=owner, provider="gdrive",
                access_token_enc="enc", folder_name="Drive",
            )
            db.session.add(conn)
            db.session.flush()
            doc = CloudDocument(
                connection_id=conn.id, user_id=owner, remote_id="f3",
                name="x.pdf", chunk_count=0,
            )
            db.session.add(doc)
            db.session.commit()
            doc_id = doc.id
        _login(client, intruder)
        r = client.post("/user/ma-bibliotheque/cloud-doc/remove-index",
                        json={"doc_id": doc_id})
        assert r.status_code == 404


# ────────────────────────────────────────────────────────────────────
# doc_chat — Pro gating
# ────────────────────────────────────────────────────────────────────

class TestDocChat:
    def test_non_subscriber_redirected(self, client, db, app):
        uid = _mk_non_subscriber_user(db, app)
        _login(client, uid)
        # Unconfirmed → fails is_subscriber → _require_subscriber redirects first.
        r = client.get("/user/ma-bibliotheque/chat/cloud/1", follow_redirects=False)
        assert r.status_code in (301, 302)

    def test_invalid_doc_type_returns_400(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.get("/user/ma-bibliotheque/chat/garbage/1")
        assert r.status_code == 400

    def test_unknown_cloud_doc_returns_404(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.get("/user/ma-bibliotheque/chat/cloud/9999")
        assert r.status_code == 404

    def test_unknown_user_doc_returns_404(self, client, db, app):
        uid = _mk_pro_user(db, app)
        _login(client, uid)
        r = client.get("/user/ma-bibliotheque/chat/user/9999")
        assert r.status_code == 404

    def test_cloud_chat_renders_template_for_owner(self, client, db, app):
        """Regression guard: the rag_search.html template used to crash
        with KeyError('date') when rendered without freshness data
        because it had `_("Newest source: %(date)s")` which Jinja's
        gettext post-formats with a (missing) `date` key. The 4
        offending lines were dead code (no JS consumer) and were
        removed; this test renders the template end-to-end to ensure
        no equivalent placeholder bug regresses."""
        from app.models.cloud_connection import CloudConnection, CloudDocument
        uid = _mk_pro_user(db, app)
        with app.app_context():
            conn = CloudConnection(
                user_id=uid, provider="gdrive",
                access_token_enc="enc", folder_name="Drive",
            )
            db.session.add(conn)
            db.session.flush()
            doc = CloudDocument(
                connection_id=conn.id, user_id=uid, remote_id="cf1",
                name="HappyPath.pdf", chunk_count=3,
            )
            db.session.add(doc)
            db.session.commit()
            doc_id = doc.id
        _login(client, uid)
        r = client.get(f"/user/ma-bibliotheque/chat/cloud/{doc_id}")
        # Must render fully — 200 OK, not 500.
        assert r.status_code == 200
        assert b"HappyPath.pdf" in r.data or b"document_context" in r.data \
            or b"<html" in r.data.lower()

    def test_user_doc_owned_by_someone_else_returns_404(self, client, db, app):
        """A user_doc id belonging to another user is rejected."""
        from app.models.user_document import UserDocument
        owner = _mk_pro_user(db, app)
        intruder = _mk_pro_user(db, app)
        with app.app_context():
            import uuid as _uuid
            doc = UserDocument(
                user_id=owner, filename="x.pdf",
                stored_name=f"stored-{_uuid.uuid4().hex}",
                file_size=10,
            )
            db.session.add(doc)
            db.session.commit()
            doc_id = doc.id
        _login(client, intruder)
        r = client.get(f"/user/ma-bibliotheque/chat/user/{doc_id}")
        assert r.status_code == 404
