"""Coverage for app/routes/user_tools.py — newsletter frequency,
data export, account deletion. Existing test_user_tools_extra.py
covers the unsubscribe path only.
"""
import uuid
from io import BytesIO
from unittest.mock import patch

import pytest

from tests.helpers import login


@pytest.fixture
def standard_user(app, db):
    from app.models.user import User
    with app.app_context():
        u = User(
            email=f"tools-{uuid.uuid4().hex[:6]}@example.com",
            firstname="T", lastname="L",
            is_confirmed=True, is_active=True,
            abonnement_type="gratuit",
            twofa_method="email",
        )
        u.set_password("ToolsUser1!")
        db.session.add(u)
        db.session.commit()
        user_id = u.id
    yield user_id
    with app.app_context():
        u = db.session.get(User, user_id)
        if u:
            db.session.delete(u)
        db.session.commit()


@pytest.fixture
def pro_user(app, db):
    from app.models.user import User
    with app.app_context():
        u = User(
            email=f"tools-pro-{uuid.uuid4().hex[:6]}@example.com",
            firstname="T", lastname="P",
            is_confirmed=True, is_active=True,
            abonnement_type="premium_pro",
            twofa_method="email",
        )
        u.set_password("ToolsPro1!")
        db.session.add(u)
        db.session.commit()
        user_id = u.id
    yield user_id
    with app.app_context():
        u = db.session.get(User, user_id)
        if u:
            db.session.delete(u)
        db.session.commit()


def _email(app, db, user_id):
    from app.models.user import User
    with app.app_context():
        return db.session.get(User, user_id).email


# ─── update-newsletter-frequency ─────────────────────────────────────

class TestUpdateNewsletterFrequency:
    def test_invalid_frequency_400(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ToolsPro1!")
        r = client.post("/user/update-newsletter-frequency",
                        json={"frequency": "yearly"})
        assert r.status_code == 400

    def test_standard_user_can_set_active_freq(
        self, client, app, db, standard_user,
    ):
        # OSS single-user build: all cadences are unlocked for every user
        # (is_pro_subscriber is always True), so a gratuit account can now
        # set an active cadence instead of being refused with 403.
        login(client, _email(app, db, standard_user), "ToolsUser1!")
        r = client.post("/user/update-newsletter-frequency",
                        json={"frequency": "daily"})
        assert r.status_code == 200

    def test_standard_user_can_set_none(self, client, app, db, standard_user):
        login(client, _email(app, db, standard_user), "ToolsUser1!")
        r = client.post("/user/update-newsletter-frequency",
                        json={"frequency": "none"})
        assert r.status_code == 200

    def test_pro_user_can_set_active_freq(self, client, app, db, pro_user):
        from app.models.user import User
        login(client, _email(app, db, pro_user), "ToolsPro1!")
        r = client.post("/user/update-newsletter-frequency",
                        json={"frequency": "weekly"})
        assert r.status_code == 200
        with app.app_context():
            assert db.session.get(User, pro_user).newsletter_frequency == "weekly"


# ─── export-my-data ──────────────────────────────────────────────────

class TestExportMyData:
    def test_returns_zip_attachment(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "ToolsPro1!")
        with patch("app.services.data_export.build_user_export",
                   return_value=BytesIO(b"PK\x03\x04zipdata")):
            r = client.get("/user/export-my-data")
        assert r.status_code == 200
        assert r.mimetype == "application/zip"


# ─── delete_account ──────────────────────────────────────────────────

class TestDeleteAccount:
    def test_deletes_user_and_redirects(self, client, app, db, standard_user):
        from app.models.user import User
        login(client, _email(app, db, standard_user), "ToolsUser1!")
        with patch("app.utils.mailer.send_email_via_brevo_api"):
            r = client.post(
                "/user/delete_account",
                data={"current_password": "ToolsUser1!"},  # Bug L11
                follow_redirects=False,
            )
        assert r.status_code in (301, 302)
        with app.app_context():
            assert db.session.get(User, standard_user) is None

    def test_wrong_password_blocks_delete(self, client, app, db, standard_user):
        """Bug L11 — mauvais mot de passe doit refuser la suppression."""
        from app.models.user import User
        login(client, _email(app, db, standard_user), "ToolsUser1!")
        r = client.post(
            "/user/delete_account",
            data={"current_password": "wrong-password"},
            follow_redirects=False,
        )
        assert r.status_code in (301, 302)
        with app.app_context():
            assert db.session.get(User, standard_user) is not None
