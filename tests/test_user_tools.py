"""Tests for user_tools routes: unsubscribe, update_favorite_tag, export_my_data, delete_account."""
import json
from unittest.mock import patch

from itsdangerous import URLSafeSerializer

from tests.helpers import login


def _login_alice(client):
    login(client, "alice@example.com", "Alice1234!")


# ── unsubscribe ───────────────────────────────────────────────────────────────

def test_unsubscribe_invalid_token_redirects(client, app):
    r = client.get("/user/unsubscribe/bad-token", follow_redirects=False)
    assert r.status_code in (301, 302)


def test_unsubscribe_valid_token(client, app, confirmed_user):
    with app.app_context():
        s = URLSafeSerializer(app.config["SECRET_KEY"], salt="unsubscribe")
        token = s.dumps({"user_id": confirmed_user.id})
    r = client.get(f"/user/unsubscribe/{token}", follow_redirects=False)
    assert r.status_code in (301, 302)
    # re-activate user so fixture teardown works
    with app.app_context():
        from app.ext import db
        from app.models.user import User
        u = db.session.get(User, confirmed_user.id)
        if u:
            u.is_active = True
            db.session.commit()


# ── export_my_data ────────────────────────────────────────────────────────────

def test_export_my_data_requires_login(client):
    r = client.get("/user/export-my-data", follow_redirects=False)
    assert r.status_code in (301, 302)


def test_export_my_data_returns_zip(client, confirmed_user):
    """The export now returns a ZIP archive with a manifest + per-section JSON
    files (profile, conversations, RAG chunks, …) — see services/data_export.py.
    """
    import io
    import zipfile

    _login_alice(client)
    r = client.get("/user/export-my-data")
    assert r.status_code == 200
    assert r.content_type == "application/zip"

    with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
        names = set(zf.namelist())
        # Core sections must exist
        assert "manifest.json" in names
        assert "profile.json" in names
        assert "documents.json" in names
        assert "conversations.json" in names
        assert "rag_chunks.json" in names
        profile = json.loads(zf.read("profile.json"))
        assert profile["email"] == "alice@example.com"
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["user_id"] == profile["id"]
    client.get("/logout")


# ── delete_account ────────────────────────────────────────────────────────────

def test_delete_account_requires_login(client):
    r = client.post("/user/delete_account", follow_redirects=False)
    assert r.status_code in (301, 302)


@patch("app.utils.mailer.send_email_via_brevo_api")
def test_delete_account_success(mock_send, client, app, db):
    """Create and immediately delete a dedicated user so confirmed_user fixture stays intact."""
    from app.models.user import User

    user = User(
        firstname="Delete",
        lastname="Me",
        email="delete_me@example.com",
        is_admin=False,
        is_confirmed=True,
        abonnement_type="gratuit",
        twofa_method="email",
    )
    user.set_password("Delete1!")
    db.session.add(user)
    db.session.commit()

    login(client, "delete_me@example.com", "Delete1!")
    r = client.post(
        "/user/delete_account",
        data={"current_password": "Delete1!"},  # Bug L11
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)
    # User should be gone
    with app.app_context():
        assert User.query.filter_by(email="delete_me@example.com").first() is None
