"""Coverage push — admin manage_users actions.

Each route is a thin POST handler that:
  1. Loads the user (404 if missing)
  2. Mutates a field
  3. Logs an audit row
  4. Commits + redirects
We exercise the happy path and the missing-user path for each.
"""
from __future__ import annotations

import uuid


def _mk_admin(db, app):
    """Create + login a superadmin via session."""
    from app.models.user import User
    with app.app_context():
        admin = User(
            email=f"adm-{uuid.uuid4().hex[:8]}@test.local",
            is_admin=True, is_confirmed=True, is_active=True,
            twofa_method="totp", totp_secret="JBSWY3DPEHPK3PXP",
            abonnement_type="gratuit",
        )
        admin.set_password("x")
        db.session.add(admin)
        db.session.commit()
        return admin.id


def _mk_target_user(db, app, *, email=None, plan="gratuit"):
    from app.models.user import User
    with app.app_context():
        u = User(
            email=email or f"tgt-{uuid.uuid4().hex[:8]}@test.local",
            is_confirmed=True, is_active=True,
            abonnement_type=plan,
        )
        u.set_password("x")
        db.session.add(u)
        db.session.commit()
        return u.id


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["admin_otp_verified"] = True  # bypass admin OTP if checked


# ────────────────────────────────────────────────────────────────────
# delete_user
# ────────────────────────────────────────────────────────────────────

class TestDeleteUser:
    def test_deletes_existing_user(self, client, db, app):
        admin_id = _mk_admin(db, app)
        target_id = _mk_target_user(db, app)
        _login(client, admin_id)
        r = client.post(f"/admin/delete_user/{target_id}", follow_redirects=False)
        assert r.status_code in (302, 303)
        # Confirm row gone.
        from app.models.user import User
        with app.app_context():
            assert db.session.get(User, target_id) is None

    def test_404_on_unknown_user(self, client, db, app):
        admin_id = _mk_admin(db, app)
        _login(client, admin_id)
        r = client.post("/admin/delete_user/999999", follow_redirects=False)
        assert r.status_code == 404


# ────────────────────────────────────────────────────────────────────
# set_user_plan
# ────────────────────────────────────────────────────────────────────

class TestSetUserPlan:
    def test_sets_premium_pro(self, client, db, app):
        admin_id = _mk_admin(db, app)
        target_id = _mk_target_user(db, app)
        _login(client, admin_id)
        r = client.post(f"/admin/user/{target_id}/set-plan",
                        data={"plan": "premium_pro"})
        assert r.status_code in (302, 303)
        from app.models.user import User
        with app.app_context():
            u = db.session.get(User, target_id)
            assert u.abonnement_type == "premium_pro"

    def test_unknown_plan_falls_back_to_pro(self, client, db, app):
        # set_user_plan only accepts premium_pro / enterprise; any other
        # value (incl. the retired "team") falls back to premium_pro.
        admin_id = _mk_admin(db, app)
        target_id = _mk_target_user(db, app)
        _login(client, admin_id)
        r = client.post(f"/admin/user/{target_id}/set-plan",
                        data={"plan": "garbage"})
        assert r.status_code in (302, 303)
        from app.models.user import User
        with app.app_context():
            assert db.session.get(User, target_id).abonnement_type == "premium_pro"

    def test_unknown_user_redirects(self, client, db, app):
        admin_id = _mk_admin(db, app)
        _login(client, admin_id)
        r = client.post("/admin/user/999999/set-plan",
                        data={"plan": "premium_pro"}, follow_redirects=False)
        assert r.status_code in (302, 303)


# ────────────────────────────────────────────────────────────────────
# edit_user_name
# ────────────────────────────────────────────────────────────────────

class TestEditUserName:
    def test_updates_first_and_last_name(self, client, db, app):
        admin_id = _mk_admin(db, app)
        target_id = _mk_target_user(db, app)
        _login(client, admin_id)
        r = client.post(f"/admin/user/{target_id}/edit-name",
                        data={"firstname": "Alice", "lastname": "Smith"})
        assert r.status_code in (302, 303)
        from app.models.user import User
        with app.app_context():
            u = db.session.get(User, target_id)
            assert u.firstname == "Alice"
            assert u.lastname == "Smith"

    def test_blank_values_clear_to_none(self, client, db, app):
        admin_id = _mk_admin(db, app)
        target_id = _mk_target_user(db, app)
        _login(client, admin_id)
        r = client.post(f"/admin/user/{target_id}/edit-name",
                        data={"firstname": "  ", "lastname": ""})
        assert r.status_code in (302, 303)
        from app.models.user import User
        with app.app_context():
            u = db.session.get(User, target_id)
            assert u.firstname is None
            assert u.lastname is None

    def test_unknown_user(self, client, db, app):
        admin_id = _mk_admin(db, app)
        _login(client, admin_id)
        r = client.post("/admin/user/999999/edit-name",
                        data={"firstname": "X"}, follow_redirects=False)
        assert r.status_code in (302, 303)


# ────────────────────────────────────────────────────────────────────
# reset_user_2fa
# ────────────────────────────────────────────────────────────────────

class TestResetUser2FA:
    def test_clears_totp_secret(self, client, db, app):
        admin_id = _mk_admin(db, app)
        target_id = _mk_target_user(db, app)
        from app.models.user import User
        with app.app_context():
            u = db.session.get(User, target_id)
            u.twofa_method = "totp"
            u.totp_secret = "JBSWY3DPEHPK3PXP"
            db.session.commit()
        _login(client, admin_id)
        r = client.post(f"/admin/user/{target_id}/reset-2fa")
        assert r.status_code in (302, 303)
        with app.app_context():
            u = db.session.get(User, target_id)
            assert u.twofa_method is None
            assert u.totp_secret is None

    def test_unknown_user(self, client, db, app):
        admin_id = _mk_admin(db, app)
        _login(client, admin_id)
        r = client.post("/admin/user/999999/reset-2fa", follow_redirects=False)
        assert r.status_code in (302, 303)


# ────────────────────────────────────────────────────────────────────
# toggle_stats_exclusion
# ────────────────────────────────────────────────────────────────────

class TestToggleStatsExclusion:
    def test_toggles_field(self, client, db, app):
        admin_id = _mk_admin(db, app)
        target_id = _mk_target_user(db, app)
        _login(client, admin_id)
        from app.models.user import User
        with app.app_context():
            assert db.session.get(User, target_id).exclude_from_stats is False
        r = client.post(f"/admin/user/{target_id}/toggle-stats-exclusion")
        assert r.status_code in (302, 303)
        with app.app_context():
            assert db.session.get(User, target_id).exclude_from_stats is True
        # toggling again flips back
        client.post(f"/admin/user/{target_id}/toggle-stats-exclusion")
        with app.app_context():
            assert db.session.get(User, target_id).exclude_from_stats is False

    def test_unknown_user(self, client, db, app):
        admin_id = _mk_admin(db, app)
        _login(client, admin_id)
        r = client.post("/admin/user/999999/toggle-stats-exclusion",
                        follow_redirects=False)
        assert r.status_code in (302, 303)


# ────────────────────────────────────────────────────────────────────
# edit_user_email
# ────────────────────────────────────────────────────────────────────

class TestEditUserEmail:
    def test_updates_email(self, client, db, app):
        admin_id = _mk_admin(db, app)
        target_id = _mk_target_user(db, app)
        _login(client, admin_id)
        new_email = f"new-{uuid.uuid4().hex[:8]}@test.local"
        r = client.post(f"/admin/user/{target_id}/edit-email",
                        data={"email": new_email})
        assert r.status_code in (302, 303)
        from app.models.user import User
        with app.app_context():
            assert db.session.get(User, target_id).email == new_email

    def test_blank_email_rejected(self, client, db, app):
        admin_id = _mk_admin(db, app)
        target_id = _mk_target_user(db, app, email="orig@test.local")
        _login(client, admin_id)
        r = client.post(f"/admin/user/{target_id}/edit-email",
                        data={"email": "  "})
        assert r.status_code in (302, 303)
        from app.models.user import User
        with app.app_context():
            assert db.session.get(User, target_id).email == "orig@test.local"

    def test_404_on_unknown_user(self, client, db, app):
        admin_id = _mk_admin(db, app)
        _login(client, admin_id)
        r = client.post("/admin/user/999999/edit-email",
                        data={"email": "x@x.local"}, follow_redirects=False)
        assert r.status_code == 404


# ────────────────────────────────────────────────────────────────────
# create_user
# ────────────────────────────────────────────────────────────────────

class TestCreateUser:
    def test_creates_user_with_form_data(self, client, db, app):
        admin_id = _mk_admin(db, app)
        _login(client, admin_id)
        new_email = f"created-{uuid.uuid4().hex[:8]}@test.local"
        r = client.post("/admin/create_user", data={
            "firstname": "New", "lastname": "User",
            "email": new_email, "password": "TempPass1!",
            "plan": "premium_pro",
        }, follow_redirects=False)
        assert r.status_code in (302, 303)
        from app.models.user import User
        with app.app_context():
            u = User.query.filter_by(email=new_email).first()
            assert u is not None

    def test_blank_email_rejected(self, client, db, app):
        admin_id = _mk_admin(db, app)
        _login(client, admin_id)
        r = client.post("/admin/create_user", data={
            "email": "", "password": "TempPass1!",
        }, follow_redirects=False)
        # Either redirected back to manage_users (with flash) or 400.
        assert r.status_code in (302, 303, 400)
