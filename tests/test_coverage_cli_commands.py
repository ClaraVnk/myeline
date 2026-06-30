"""Coverage push — Flask CLI commands (commands.py / commands_cleanup.py)."""
from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner


def _mk_user(db, app, **overrides):
    from app.models.user import User
    with app.app_context():
        defaults = dict(
            email=f"cli-{uuid.uuid4().hex[:8]}@test.local",
            is_admin=False, is_active=True, is_confirmed=True,
            abonnement_type="gratuit",
        )
        defaults.update(overrides)
        u = User(**defaults)
        u.set_password("x")
        db.session.add(u)
        db.session.commit()
        return u.id


# ────────────────────────────────────────────────────────────────────
# cleanup-unconfirmed-accounts
# ────────────────────────────────────────────────────────────────────

class TestCleanupUnconfirmed:
    def test_no_accounts_to_delete(self, app):
        from app.commands import cleanup_unconfirmed_accounts
        runner = CliRunner()
        with app.app_context():
            r = runner.invoke(cleanup_unconfirmed_accounts)
        assert r.exit_code == 0
        assert "No unconfirmed" in r.output

    def test_deletes_old_unconfirmed_accounts(self, app, db):
        from app.commands import cleanup_unconfirmed_accounts
        from app.models.user import User

        # Create an old unconfirmed user
        with app.app_context():
            old = User(
                email=f"old-{uuid.uuid4().hex[:8]}@test.local",
                is_confirmed=False, is_active=True,
                abonnement_type="gratuit",
            )
            old.set_password("x")
            db.session.add(old)
            db.session.commit()
            # Force created_at into the past via raw update
            from sqlalchemy import text
            db.session.execute(
                text("UPDATE users SET created_at = :ts WHERE id = :id"),
                {"ts": datetime.utcnow() - timedelta(days=60), "id": old.id},
            )
            db.session.commit()
            uid = old.id

        runner = CliRunner()
        with app.app_context():
            r = runner.invoke(cleanup_unconfirmed_accounts)
        assert r.exit_code == 0
        with app.app_context():
            assert db.session.get(User, uid) is None


# ────────────────────────────────────────────────────────────────────
# anonymize-inactive-accounts
# ────────────────────────────────────────────────────────────────────

class TestAnonymizeInactive:
    def test_no_inactive_accounts(self, app):
        from app.commands import anonymize_inactive_accounts
        runner = CliRunner()
        with app.app_context():
            r = runner.invoke(anonymize_inactive_accounts)
        assert r.exit_code == 0
        assert "No inactive" in r.output


# ────────────────────────────────────────────────────────────────────
# show-retention-stats
# ────────────────────────────────────────────────────────────────────

class TestRetentionStats:
    def test_renders_stats_block(self, app, db):
        from app.commands import show_retention_stats
        _mk_user(db, app, is_confirmed=True)
        _mk_user(db, app, is_confirmed=False)
        runner = CliRunner()
        with app.app_context():
            r = runner.invoke(show_retention_stats)
        assert r.exit_code == 0
        assert "Total accounts" in r.output
        assert "Unconfirmed" in r.output


# ────────────────────────────────────────────────────────────────────
# cleanup-old-logs
# ────────────────────────────────────────────────────────────────────

class TestCleanupOldLogs:
    def test_warns_when_logs_folder_absent(self, app, tmp_path, monkeypatch):
        """When logs/ doesn't exist, command exits cleanly with a warning."""
        from app.commands_cleanup import cleanup_old_logs
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        with app.app_context():
            r = runner.invoke(cleanup_old_logs)
        assert r.exit_code == 0
        assert "Logs folder does not exist" in r.output

    def test_deletes_old_logs(self, app, tmp_path, monkeypatch):
        """Old .log files past the cutoff are removed; payment logs survive."""
        from app.commands_cleanup import cleanup_old_logs
        monkeypatch.chdir(tmp_path)
        logs = tmp_path / "logs"
        logs.mkdir()
        old = logs / "old.log"
        old.write_text("ancient")
        os.utime(old, (0, 0))  # epoch 1970 — definitely past cutoff
        # Payment log (must NOT be deleted, regardless of mtime)
        payment = logs / "payment.log"
        payment.write_text("keep me")
        os.utime(payment, (0, 0))
        # Recent log (must NOT be deleted)
        recent = logs / "fresh.log"
        recent.write_text("keep")
        runner = CliRunner()
        with app.app_context():
            r = runner.invoke(cleanup_old_logs, ["--days", "180"])
        assert r.exit_code == 0
        assert not old.exists()
        assert payment.exists()
        assert recent.exists()


class TestShowCleanupStats:
    def test_runs_without_logs_folder(self, app, tmp_path, monkeypatch):
        from app.commands_cleanup import show_cleanup_stats
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        with app.app_context():
            r = runner.invoke(show_cleanup_stats)
        assert r.exit_code == 0
        assert "Cleanup Statistics" in r.output


# ────────────────────────────────────────────────────────────────────
# admin CLI commands (in app/__init__.py)
# ────────────────────────────────────────────────────────────────────

class TestAdminCommands:
    def test_create_admin_command_registered(self, app):
        with app.app_context():
            assert "create-admin" in app.cli.commands

    def test_update_admin_command_registered(self, app):
        with app.app_context():
            assert "update-admin" in app.cli.commands

    def test_delete_user_command_registered(self, app):
        with app.app_context():
            assert "delete-user" in app.cli.commands
