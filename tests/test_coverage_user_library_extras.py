"""Coverage push — user_library remaining gaps (edge cases)."""
from __future__ import annotations

import uuid
from unittest.mock import patch


def _mk_pro(db, app, *, plan="premium_pro"):
    from app.models.user import User
    with app.app_context():
        u = User(
            email=f"u-{uuid.uuid4().hex[:8]}@test.local",
            is_confirmed=True, is_active=True,
            twofa_method="totp", totp_secret="JBSWY3DPEHPK3PXP",
            abonnement_type=plan,
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
# Library list with pagination + filters
# ────────────────────────────────────────────────────────────────────

class TestPersonalLibraryEdges:
    def test_invalid_page_falls_back_gracefully(self, client, db, app):
        uid = _mk_pro(db, app)
        _login(client, uid)
        r = client.get("/user/ma-bibliotheque?page=-5&per_page=50")
        assert r.status_code == 200

    def test_with_all_filter_params(self, client, db, app):
        uid = _mk_pro(db, app)
        _login(client, uid)
        # Mix every filter — exercises the merged-list branch.
        r = client.get(
            "/user/ma-bibliotheque?q=research&tag=ai&provider=gdrive"
            "&source=&per_page=10&page=2"
        )
        assert r.status_code == 200


# ────────────────────────────────────────────────────────────────────
# Cloud preview with chunks
# ────────────────────────────────────────────────────────────────────

class TestCloudPreviewWithChunks:
    def test_cloud_preview_falls_back_when_chroma_fails(self, client, db, app):
        from app.models.cloud_connection import CloudConnection, CloudDocument
        uid = _mk_pro(db, app)
        with app.app_context():
            conn = CloudConnection(
                user_id=uid, provider="gdrive",
                access_token_enc="enc", folder_name="Drive",
            )
            db.session.add(conn)
            db.session.flush()
            doc = CloudDocument(
                connection_id=conn.id, user_id=uid, remote_id="r1",
                name="X.pdf", mime_type="application/pdf",
                file_size=99, chunk_count=3,
            )
            db.session.add(doc)
            db.session.commit()
            doc_id = doc.id
        _login(client, uid)
        with patch("app.services.rag_service._get_user_collection",
                   side_effect=RuntimeError("chroma down")):
            r = client.get(f"/user/ma-bibliotheque/preview/cloud/{doc_id}")
        assert r.status_code == 200
        body = r.get_json()
        assert body["chunk_count"] == 3
        assert body["chunks"] == []
