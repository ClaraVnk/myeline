"""Tests for app/commands_cleanup.py CLI commands."""


def test_cleanup_old_logs_default(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["cleanup-old-logs"])
    assert result.exit_code == 0


def test_cleanup_old_logs_custom_days(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["cleanup-old-logs", "--days", "30"])
    assert result.exit_code == 0


def test_show_cleanup_stats(app, db):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["show-cleanup-stats"])
    assert result.exit_code == 0
