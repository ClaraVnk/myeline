"""Unit tests for app/tasks.py — call tasks directly, mock _make_app."""
from unittest.mock import patch


# ── broadcast_task ────────────────────────────────────────────────────────────

def test_broadcast_task_empty_recipients(app):
    with patch("app.tasks._make_app", return_value=app):
        from app.tasks import broadcast_task
        broadcast_task([], "Subject", "<p>Body</p>", "http://localhost/")


def test_broadcast_task_with_recipient(app, db):
    from app.models.user import User

    user = User(
        firstname="Broad",
        lastname="Cast",
        email="task_broadcast@example.com",
        is_confirmed=True,
        is_active=True,
        newsletter_frequency="weekly",
        abonnement_type="premium_pro",
    )
    user.set_password("Broad123!")
    db.session.add(user)
    db.session.commit()

    with patch("app.tasks._make_app", return_value=app), \
         patch("app.utils.mailer.send_email_via_brevo_api", return_value=None):
        from app.tasks import broadcast_task
        broadcast_task([user.id], "Subject", "<p>Body</p>", "http://localhost/")

    db.session.delete(user)
    db.session.commit()
