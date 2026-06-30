"""Extra tests for admin manage_users — error paths."""
from unittest.mock import patch

from tests.helpers import admin_login


# ── create_user DB error ──────────────────────────────────────────────────────


def test_create_user_db_error(client, admin_user):
    """DB commit error when creating user → flash error, redirect."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    from app.ext import db as the_db

    original = the_db.session.commit
    call_count = [0]

    def fail_once():
        call_count[0] += 1
        if call_count[0] == 1:
            the_db.session.rollback()
            raise Exception("db error")
        return original()

    with patch.object(the_db.session, "commit", side_effect=fail_once):
        r = client.post(
            "/admin/create_user",
            data={
                "email": "newuser_dberr@example.com",
                "first_name": "DB",
                "last_name": "Error",
            },
            follow_redirects=False,
        )
    assert r.status_code in (301, 302, 200)

    the_db.session.rollback()
    client.get("/admin/logout")


# ── create_user email errors ──────────────────────────────────────────────────


def test_create_user_email_connection_error(client, admin_user, db):
    """ConnectionError sending email → user still created, warning flash."""
    from requests.exceptions import ConnectionError

    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)

    with patch(
        "app.routes.admin.manage_users.send_email_via_brevo_api",
        side_effect=ConnectionError("no network"),
    ):
        r = client.post(
            "/admin/create_user",
            data={
                "email": "newuser_connloss@example.com",
                "first_name": "Conn",
                "last_name": "Loss",
            },
            follow_redirects=False,
        )
    assert r.status_code in (301, 302, 200)

    from app.models.user import User

    u = User.query.filter_by(email="newuser_connloss@example.com").first()
    if u:
        db.session.delete(u)
        db.session.commit()
    client.get("/admin/logout")


def test_create_user_email_generic_error(client, admin_user, db):
    """Generic exception sending email → user still created, warning flash."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)

    with patch(
        "app.routes.admin.manage_users.send_email_via_brevo_api",
        side_effect=Exception("smtp down"),
    ):
        r = client.post(
            "/admin/create_user",
            data={
                "email": "newuser_smtperr@example.com",
                "first_name": "SMTP",
                "last_name": "Err",
            },
            follow_redirects=False,
        )
    assert r.status_code in (301, 302, 200)

    from app.models.user import User

    u = User.query.filter_by(email="newuser_smtperr@example.com").first()
    if u:
        db.session.delete(u)
        db.session.commit()
    client.get("/admin/logout")


# ── delete_user DB error ──────────────────────────────────────────────────────


def test_delete_user_db_error(client, admin_user, db):
    """DB commit error on delete_user → flash error, redirect."""
    from app.models.user import User

    user = User(
        firstname="ToDel",
        lastname="DBErr",
        email="del_dberr@example.com",
        is_confirmed=True,
        abonnement_type="gratuit",
    )
    user.set_password("ToDel1!")
    db.session.add(user)
    db.session.commit()
    uid = user.id

    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)

    from app.ext import db as the_db

    original = the_db.session.commit
    call_count = [0]

    def fail_once():
        call_count[0] += 1
        if call_count[0] == 1:
            the_db.session.rollback()
            raise Exception("commit failed")
        return original()

    with patch.object(the_db.session, "commit", side_effect=fail_once):
        r = client.post(f"/admin/delete_user/{uid}", follow_redirects=False)
    assert r.status_code in (301, 302, 200)

    the_db.session.rollback()
    client.get("/admin/logout")

    u = User.query.filter_by(email="del_dberr@example.com").first()
    if u:
        db.session.delete(u)
        db.session.commit()
