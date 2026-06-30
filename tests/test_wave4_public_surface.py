"""Round 5 wave 4 — public surface hardening.

A4-M1 — /login no longer distinguishes "unconfirmed account" from
        "wrong password" in its response.
A4-M2 — /search refuses oversized payloads + caps SharedRagResult sources.
A4-M4 — bounded JSON parser refuses deeply nested objects.
A2-M2 — S3 endpoint regex regression: IPv6 loopback + private hosts rejected.
"""
import json

import pytest


# ── A4-M1 — login enumeration ──────────────────────────────────────────


def test_login_unconfirmed_account_gives_generic_response(client, db, app):
    import uuid

    from app.models.user import User

    email = f"unconf-{uuid.uuid4().hex[:8]}@test.local"
    with app.app_context():
        user = User(email=email, password_hash="x", is_confirmed=False)
        user.set_password("Sup3rPass!2026")
        db.session.add(user)
        db.session.commit()
        uid = user.id

    try:
        from unittest.mock import patch
        with patch("app.routes.auth.verify_recaptcha", return_value=True):
            r = client.post(
                "/login",
                data={"email": email, "password": "Sup3rPass!2026"},
                follow_redirects=False,
            )
        # 200 with login template re-rendered — same shape as wrong-password.
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "non confirmé" not in body.lower()
        assert "resend" not in body.lower()
        assert "confirmer votre adresse" not in body.lower()
    finally:
        with app.app_context():
            u = db.session.get(User, uid)
            if u is not None:
                db.session.delete(u)
                db.session.commit()


def test_login_wrong_password_returns_login_template(client, db, app):
    """Wrong-password path re-renders the login form (no redirect, no
    different template) — and the unconfirmed-account path must do the
    same, so a caller can't distinguish the two states.
    """
    import uuid

    from app.models.user import User

    email = f"wrongpw-{uuid.uuid4().hex[:8]}@test.local"
    with app.app_context():
        user = User(email=email, password_hash="x", is_confirmed=True)
        user.set_password("Sup3rPass!2026")
        db.session.add(user)
        db.session.commit()
        uid = user.id

    try:
        from unittest.mock import patch
        with patch("app.routes.auth.verify_recaptcha", return_value=True):
            r = client.post(
                "/login",
                data={"email": email, "password": "definitely-wrong"},
                follow_redirects=False,
            )
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "non confirmé" not in body.lower()
        assert "resend" not in body.lower()
    finally:
        with app.app_context():
            u = db.session.get(User, uid)
            if u is not None:
                db.session.delete(u)
                db.session.commit()


# ── A4-M2 — /search payload caps ───────────────────────────────────────


def test_search_refuses_oversized_body(client):
    huge = {"query": "x" * 100, "junk": "y" * (16 * 1024)}
    r = client.post(
        "/search",
        data=json.dumps(huge),
        content_type="application/json",
    )
    assert r.status_code == 413


def test_search_rejects_non_dict_root(client):
    r = client.post(
        "/search",
        data=json.dumps(["not", "a", "dict"]),
        content_type="application/json",
    )
    assert r.status_code == 400


# ── A4-M4 — JSON depth bound ───────────────────────────────────────────


def test_parse_json_bounded_accepts_shallow():
    from app.utils.json_safe import parse_json_bounded
    out = parse_json_bounded('{"a": {"b": {"c": 1}}}', max_depth=5)
    assert out == {"a": {"b": {"c": 1}}}


def test_parse_json_bounded_rejects_deep():
    from app.utils.json_safe import JsonTooDeepError, parse_json_bounded

    # Construct depth=20 nested dict.
    deep = "{}"
    for _ in range(20):
        deep = '{"x": ' + deep + "}"
    with pytest.raises(JsonTooDeepError):
        parse_json_bounded(deep, max_depth=5)


def test_search_refuses_deeply_nested_json(client):
    deep_obj = {"query": "hello"}
    cur = deep_obj
    for _ in range(20):
        cur["nested"] = {}
        cur = cur["nested"]
    r = client.post(
        "/search",
        data=json.dumps(deep_obj),
        content_type="application/json",
    )
    assert r.status_code == 400


# ── A2-M2 — S3 endpoint regex regression ───────────────────────────────


def test_s3_validate_endpoint_rejects_ipv6_loopback():
    from app.services.s3_service import _validate_endpoint
    with pytest.raises(ValueError):
        _validate_endpoint("https://[::1]:9000/")


def test_s3_validate_endpoint_rejects_ipv4_loopback():
    from app.services.s3_service import _validate_endpoint
    with pytest.raises(ValueError):
        _validate_endpoint("https://127.0.0.1:9000/")


def test_s3_validate_endpoint_rejects_aws_metadata():
    from app.services.s3_service import _validate_endpoint
    with pytest.raises(ValueError):
        _validate_endpoint("https://169.254.169.254/")


def test_s3_validate_endpoint_accepts_aws():
    from app.services.s3_service import _validate_endpoint
    _validate_endpoint("https://s3.eu-west-3.amazonaws.com")  # no raise
