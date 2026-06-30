"""Extra manage_users tests — edit_user_email."""
from unittest.mock import patch

from tests.helpers import admin_login


# ── edit_user_email ───────────────────────────────────────────────────────────


@patch("app.routes.admin.manage_users.send_email_via_brevo_api")
def test_edit_user_email_post(mock_send, client, admin_user, confirmed_user):
    mock_send.return_value = None
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.post(
        f"/admin/user/{confirmed_user.id}/edit-email",
        data={"email": "alice_new@example.com"},
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)
    client.get("/admin/logout")
    # Restore original email to not break other tests
    from app.ext import db
    from app.models.user import User
    u = User.query.filter_by(email="alice_new@example.com").first()
    if u:
        u.email = "alice@example.com"
        u.is_confirmed = True
        db.session.commit()


def test_edit_user_email_post_empty(client, admin_user, confirmed_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.post(
        f"/admin/user/{confirmed_user.id}/edit-email",
        data={"email": ""},
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)
    client.get("/admin/logout")


def test_edit_user_email_not_found(client, admin_user):
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.post("/admin/user/99999/edit-email", data={"email": "x@example.com"})
    assert r.status_code == 404
    client.get("/admin/logout")
