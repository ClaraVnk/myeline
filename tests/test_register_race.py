"""Coverage for the IntegrityError handling on POST /register
(audit round 4, A1-H2).

Two concurrent registration POSTs with the same email used to both pass
the form validation and race on the INSERT — one row landed, the second
raised IntegrityError, and the user saw an opaque 500. We now catch the
IntegrityError, roll back, and redirect to /login with a friendly
"compte déjà existant" flash so the user has a clear path forward.
"""
from unittest.mock import patch


def _register_form_data(email="race@example.test"):
    return {
        "firstname": "Race",
        "lastname": "User",
        "email": email,
        "password": "StrongPass-12345!",
        "confirm_password": "StrongPass-12345!",
        "accept_cgv": "y",
        "accept_privacy": "y",
        "g-recaptcha-response": "ok",
        "plan": "standard",
    }


def test_register_handles_integrity_error_on_concurrent_email(client, db):
    """When the UNIQUE constraint on email rejects the INSERT, the route
    must rollback + redirect to /login, not crash.

    A real race condition is impossible to set up deterministically in a
    single-thread test: the in-form ``email_unique`` validator catches a
    pre-inserted row before commit() ever runs. We instead mock the
    commit call to raise IntegrityError, simulating the case where two
    concurrent requests both pass the validator and only collide at the
    database write — that's the actual A1-H2 footprint.
    """
    from sqlalchemy.exc import IntegrityError

    from app.ext import db as _db

    target = "race-concurrent@example.com"
    real_commit = _db.session.commit
    raised = {"once": False}

    def _commit_raises_once():
        if not raised["once"]:
            raised["once"] = True
            raise IntegrityError("statement", {}, Exception("uq_users_email"))
        return real_commit()

    with patch("app.routes.auth.verify_recaptcha", return_value=True), \
         patch("app.ext.db.session.commit", side_effect=_commit_raises_once):
        r = client.post(
            "/register",
            data=_register_form_data(target),
            follow_redirects=False,
        )

    assert r.status_code == 302, r.data.decode()[:1500]
    assert "/login" in r.headers.get("Location", "")
