"""Tests for user ticket routes: list, new, detail, reply."""
from unittest.mock import patch

import pytest

from tests.helpers import login


def _login_alice(client):
    login(client, "alice@example.com", "Alice1234!")


# ── Auth guards ───────────────────────────────────────────────────────────────


def test_ticket_list_requires_login(client):
    r = client.get("/user/tickets", follow_redirects=False)
    assert r.status_code in (301, 302)


def test_ticket_new_requires_login(client):
    r = client.get("/user/tickets/new", follow_redirects=False)
    assert r.status_code in (301, 302)


# ── Ticket list ───────────────────────────────────────────────────────────────


def test_ticket_list_empty(client, confirmed_user):
    _login_alice(client)
    r = client.get("/user/tickets")
    assert r.status_code == 200
    client.get("/logout")


# ── Ticket new ────────────────────────────────────────────────────────────────


def test_ticket_new_get(client, confirmed_user):
    _login_alice(client)
    r = client.get("/user/tickets/new")
    assert r.status_code == 200
    client.get("/logout")


def test_ticket_new_post_success(client, confirmed_user, db):
    _login_alice(client)
    r = client.post(
        "/user/tickets/new",
        data={"subject": "Mon problème", "body": "Description complète du problème."},
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)
    client.get("/logout")

    from app.models.support_ticket import SupportTicket
    ticket = SupportTicket.query.filter_by(user_id=confirmed_user.id).first()
    assert ticket is not None
    assert ticket.subject == "Mon problème"
    db.session.delete(ticket)
    db.session.commit()


def test_ticket_new_post_missing_subject(client, confirmed_user):
    _login_alice(client)
    r = client.post(
        "/user/tickets/new",
        data={"subject": "", "body": "Description."},
    )
    assert r.status_code == 200
    client.get("/logout")


def test_ticket_new_post_missing_body(client, confirmed_user):
    _login_alice(client)
    r = client.post(
        "/user/tickets/new",
        data={"subject": "Sujet", "body": ""},
    )
    assert r.status_code == 200
    client.get("/logout")


def test_ticket_new_post_subject_too_long(client, confirmed_user):
    _login_alice(client)
    r = client.post(
        "/user/tickets/new",
        data={"subject": "x" * 256, "body": "Description."},
    )
    assert r.status_code == 200
    client.get("/logout")


# ── Ticket detail ─────────────────────────────────────────────────────────────


def test_ticket_detail_get(client, confirmed_user, db):
    from app.models.support_ticket import SupportTicket, TicketMessage

    ticket = SupportTicket(user_id=confirmed_user.id, subject="Test ticket")
    db.session.add(ticket)
    db.session.flush()
    msg = TicketMessage(ticket_id=ticket.id, author_id=confirmed_user.id, is_staff=False, body="Msg")
    db.session.add(msg)
    db.session.commit()

    _login_alice(client)
    r = client.get(f"/user/tickets/{ticket.id}")
    assert r.status_code == 200
    client.get("/logout")

    db.session.delete(ticket)
    db.session.commit()


def test_ticket_detail_not_found(client, confirmed_user):
    _login_alice(client)
    r = client.get("/user/tickets/999999")
    assert r.status_code == 404
    client.get("/logout")


def test_ticket_detail_wrong_user(client, confirmed_user, db, app):
    """User cannot access another user's ticket."""
    from app.models.support_ticket import SupportTicket
    from app.models.user import User

    other = User(
        firstname="Other", lastname="User", email="other_ticket@example.com",
        is_confirmed=True, abonnement_type="gratuit",
    )
    other.set_password("Other1234!")
    db.session.add(other)
    db.session.commit()

    ticket = SupportTicket(user_id=other.id, subject="Other's ticket")
    db.session.add(ticket)
    db.session.commit()

    _login_alice(client)
    r = client.get(f"/user/tickets/{ticket.id}")
    assert r.status_code == 404
    client.get("/logout")

    db.session.delete(ticket)
    db.session.delete(other)
    db.session.commit()


def test_ticket_reply(client, confirmed_user, db):
    from app.models.support_ticket import SupportTicket, TicketMessage

    ticket = SupportTicket(user_id=confirmed_user.id, subject="Reply test")
    db.session.add(ticket)
    db.session.commit()

    _login_alice(client)
    r = client.post(
        f"/user/tickets/{ticket.id}",
        data={"body": "Ma réponse."},
        follow_redirects=False,
    )
    assert r.status_code in (301, 302)
    client.get("/logout")

    msgs = TicketMessage.query.filter_by(ticket_id=ticket.id).all()
    assert len(msgs) == 1
    assert msgs[0].body == "Ma réponse."

    db.session.delete(ticket)
    db.session.commit()
