"""Tests for Flask CLI commands — commands.py and commands_cleanup.py."""


# ── commands.py — GDPR cleanup commands ──────────────────────────────────────

def test_cleanup_unconfirmed_no_accounts(app):
    """Command runs cleanly when there are no old unconfirmed accounts."""
    runner = app.test_cli_runner()
    result = runner.invoke(args=["cleanup-unconfirmed-accounts"])
    assert result.exit_code == 0
    assert "No unconfirmed accounts" in result.output or "Deleted" in result.output


def test_anonymize_inactive_no_accounts(app):
    """Command runs cleanly when there are no 3-year-old inactive accounts."""
    runner = app.test_cli_runner()
    result = runner.invoke(args=["anonymize-inactive-accounts"])
    assert result.exit_code == 0


def test_cleanup_unconfirmed_deletes_old_account(app, db):
    """Old unconfirmed account is deleted by the command."""
    from datetime import timedelta

    from app.models.user import User
    from app.utils.time_utils import utcnow

    old_user = User(
        firstname="Old",
        lastname="Unconfirmed",
        email="old_unconfirmed@example.com",
        is_admin=False,
        is_confirmed=False,
        abonnement_type="gratuit",
        created_at=utcnow() - timedelta(days=40),
    )
    old_user.set_password("Old12345!")
    db.session.add(old_user)
    db.session.commit()
    uid = old_user.id

    runner = app.test_cli_runner()
    result = runner.invoke(args=["cleanup-unconfirmed-accounts"])
    assert result.exit_code == 0

    with app.app_context():
        from app.ext import db as _db
        assert _db.session.get(User, uid) is None


# ── commands_cleanup.py — additional cleanup commands ────────────────────────

def test_cleanup_old_logs(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["cleanup-old-logs"])
    assert result.exit_code == 0
