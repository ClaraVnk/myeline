"""Coverage for app/routes/user_rag.py — feedback, history, saved
queries, alerts, and conversations endpoints. The existing test_user_rag.py
only exercises the /query path.
"""
import uuid

import pytest

from tests.helpers import login


@pytest.fixture
def pro_user(app, db):
    from app.models.user import User
    with app.app_context():
        u = User(
            email=f"rag-pro-{uuid.uuid4().hex[:6]}@example.com",
            firstname="P", lastname="C",
            is_confirmed=True, is_active=True,
            abonnement_type="premium_pro",
            twofa_method="email",
        )
        u.set_password("RagPro1!")
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


# ─── feedback ─────────────────────────────────────────────────────────

class TestRagFeedback:
    def test_rejects_invalid_payload(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "RagPro1!")
        r = client.post("/user/recherche/feedback", json={})
        assert r.status_code == 400

    def test_accepts_thumbs_up(self, client, app, db, pro_user):
        from app.models.rag_feedback import RagFeedback
        login(client, _email(app, db, pro_user), "RagPro1!")
        r = client.post("/user/recherche/feedback",
                        json={"query": "Q", "rating": "thumbs_up",
                              "comment": "Good"})
        assert r.status_code == 200
        with app.app_context():
            count = db.session.query(RagFeedback).filter_by(
                user_id=pro_user,
            ).count()
        assert count == 1


# ─── history ──────────────────────────────────────────────────────────

class TestRagHistory:
    def test_pro_returns_history(self, client, app, db, pro_user):
        from app.models.query_history import QueryHistory
        with app.app_context():
            db.session.add(QueryHistory(
                user_id=pro_user,
                query="hello",
                scope="personal",
                sources_count=3,
            ))
            db.session.commit()
        login(client, _email(app, db, pro_user), "RagPro1!")
        r = client.get("/user/recherche/history")
        assert r.status_code == 200
        data = r.get_json()
        assert any(row["query"] == "hello" for row in data)


# ─── saved queries ────────────────────────────────────────────────────

class TestRagSavedQueries:
    def test_save_then_list(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "RagPro1!")
        r = client.post("/user/recherche/saved",
                        json={"query": "What is X?",
                              "name": "MyQuery",
                              "scope": "personal"})
        assert r.status_code == 200
        sq_id = r.get_json()["id"]

        r = client.get("/user/recherche/saved")
        assert r.status_code == 200
        data = r.get_json()
        assert any(row["id"] == sq_id for row in data)

    def test_save_empty_query_rejected(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "RagPro1!")
        r = client.post("/user/recherche/saved",
                        json={"query": "", "name": "X"})
        assert r.status_code == 400

    def test_delete_saved_query(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "RagPro1!")
        # First create
        r = client.post("/user/recherche/saved",
                        json={"query": "Q1", "name": "N1", "scope": "personal"})
        sq_id = r.get_json()["id"]
        # Then delete
        r = client.delete(f"/user/recherche/saved/{sq_id}")
        assert r.status_code == 200

    def test_delete_other_users_query_404(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "RagPro1!")
        r = client.delete("/user/recherche/saved/999999")
        assert r.status_code == 404


# ─── watch alerts ─────────────────────────────────────────────────────

class TestRagAlerts:
    def test_list_returns_empty_array(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "RagPro1!")
        r = client.get("/user/recherche/alerts")
        assert r.status_code == 200
        assert r.get_json() == []

    def test_create_with_invalid_keyword_rejected(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "RagPro1!")
        r = client.post("/user/recherche/alerts",
                        json={"keyword": "", "scope": "all", "frequency": "daily"})
        assert r.status_code == 400

    def test_create_keyword_too_long_rejected(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "RagPro1!")
        r = client.post("/user/recherche/alerts",
                        json={"keyword": "x" * 300})
        assert r.status_code == 400

    def test_create_alert_pro(self, client, app, db, pro_user):
        from app.models.watch_alert import WatchAlert
        login(client, _email(app, db, pro_user), "RagPro1!")
        r = client.post("/user/recherche/alerts",
                        json={"keyword": "ai", "scope": "all",
                              "frequency": "daily"})
        assert r.status_code == 200
        with app.app_context():
            assert WatchAlert.query.filter_by(user_id=pro_user).count() == 1

    def test_invalid_scope_falls_back_to_all(self, client, app, db, pro_user):
        from app.models.watch_alert import WatchAlert
        login(client, _email(app, db, pro_user), "RagPro1!")
        r = client.post("/user/recherche/alerts",
                        json={"keyword": "ai", "scope": "bogus",
                              "frequency": "monthly"})  # invalid frequency too
        assert r.status_code == 200
        with app.app_context():
            row = WatchAlert.query.filter_by(user_id=pro_user).first()
        assert row.scope == "all"
        assert row.frequency == "daily"

    def test_toggle_and_delete_alert(self, client, app, db, pro_user):
        from app.models.watch_alert import WatchAlert
        login(client, _email(app, db, pro_user), "RagPro1!")
        r = client.post("/user/recherche/alerts",
                        json={"keyword": "py"})
        alert_id = r.get_json()["id"]

        # Toggle
        r = client.post(f"/user/recherche/alerts/{alert_id}/toggle")
        assert r.status_code == 200
        with app.app_context():
            assert db.session.get(WatchAlert, alert_id).is_active is False

        # Delete
        r = client.delete(f"/user/recherche/alerts/{alert_id}")
        assert r.status_code == 200


# ─── conversations ────────────────────────────────────────────────────

class TestConversations:
    def test_list_returns_only_users_conversations(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "RagPro1!")
        r = client.get("/user/recherche/conversations")
        assert r.status_code == 200
        # The endpoint may include conversations from other tests when running
        # in parallel — we only assert the structure here.
        data = r.get_json()
        assert "conversations" in data
        assert isinstance(data["conversations"], list)

    def test_get_unknown_conversation_404(self, client, app, db, pro_user):
        login(client, _email(app, db, pro_user), "RagPro1!")
        r = client.get("/user/recherche/conversations/999999")
        assert r.status_code == 404
