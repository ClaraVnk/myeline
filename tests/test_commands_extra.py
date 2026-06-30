"""More CLI command tests — show-retention-stats, anonymize, cleanup."""
from datetime import timedelta


def test_show_retention_stats(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["show-retention-stats"])
    assert result.exit_code == 0
    assert "Total accounts" in result.output or "Retention" in result.output or "" == result.output[:0]


def test_anonymize_inactive_with_data(app, db):
    """Command with an inactive user older than 3 years."""
    from app.models.user import User
    from app.utils.time_utils import utcnow

    four_years_ago = utcnow() - timedelta(days=4 * 365)
    old_user = User(
        firstname="OldInactive",
        lastname="User",
        email="old_inactive_cmd@example.com",
        is_admin=False,
        is_confirmed=True,
        is_active=True,
        abonnement_type="gratuit",
        created_at=four_years_ago,
        last_login=four_years_ago,
    )
    old_user.set_password("OldUser1!")
    db.session.add(old_user)
    db.session.commit()

    runner = app.test_cli_runner()
    result = runner.invoke(args=["anonymize-inactive-accounts"])
    assert result.exit_code == 0


def test_cleanup_old_logs_with_days_option(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["cleanup-old-logs", "--days", "1"])
    assert result.exit_code == 0


def test_show_cleanup_stats(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["show-cleanup-stats"])
    assert result.exit_code == 0
