"""Extra tests for user_tools routes — unsubscribe, delete_account, error paths."""
import json
from unittest.mock import patch

from tests.helpers import login


def _login_alice(client):
    login(client, "alice@example.com", "Alice1234!")


# ── unsubscribe error paths ────────────────────────────────────────────────────


def test_unsubscribe_commit_error(client, confirmed_user, app, db):
    """DB commit error → rollback + redirect (no crash)."""
    from itsdangerous import URLSafeSerializer

    with app.app_context():
        s = URLSafeSerializer(app.config["SECRET_KEY"], salt="unsubscribe")
        token = s.dumps({"user_id": confirmed_user.id})

    from app.ext import db as the_db

    original = the_db.session.commit

    call_count = [0]

    def raise_on_first():
        call_count[0] += 1
        if call_count[0] == 1:
            raise Exception("db commit failed")
        return original()

    with patch.object(the_db.session, "commit", side_effect=raise_on_first):
        r = client.get(f"/user/unsubscribe/{token}", follow_redirects=False)
    assert r.status_code in (301, 302)

    # Restore user to active for fixture teardown
    with app.app_context():
        from app.models.user import User

        u = the_db.session.get(User, confirmed_user.id)
        if u and not u.is_active:
            u.is_active = True
            the_db.session.commit()


def test_unsubscribe_user_not_found(client, app):
    """Token with nonexistent user_id → 404 or redirect."""
    from itsdangerous import URLSafeSerializer

    with app.app_context():
        s = URLSafeSerializer(app.config["SECRET_KEY"], salt="unsubscribe")
        token = s.dumps({"user_id": 999999})
    r = client.get(f"/user/unsubscribe/{token}", follow_redirects=False)
    assert r.status_code in (301, 302, 404)


# ── delete_account paths ───────────────────────────────────────────────────────


@patch("app.utils.mailer.send_email_via_brevo_api", side_effect=IOError("smtp down"))
def test_delete_account_email_ioerror(mock_send, client, app, db):
    """Email IOError on account deletion → account still deleted, no crash."""
    from app.models.user import User

    user = User(
        firstname="MailFail",
        lastname="Delete",
        email="mailfail_del@example.com",
        is_confirmed=True,
        abonnement_type="gratuit",
        twofa_method="email",
    )
    user.set_password("Mail1234!")
    db.session.add(user)
    db.session.commit()

    login(client, "mailfail_del@example.com", "Mail1234!")
    r = client.post(
        "/user/delete_account",
        data={"current_password": "Mail1234!"},  # Bug L11
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)

    with app.app_context():
        assert User.query.filter_by(email="mailfail_del@example.com").first() is None
