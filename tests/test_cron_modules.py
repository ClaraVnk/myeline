"""Tests for the cron modules added during the prod-readiness sweep:
record_status, archive_audit_log, backup_databases.

The integration paths that need a Podman API or real network are
mocked at the boundary so the tests run on a plain test process.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch


# ─── record_status ────────────────────────────────────────────────────

class TestRecordStatus:
    def test_run_records_a_sample(self, app, db):
        from app.cron.record_status import run
        from app.models.status_sample import StatusSample

        with app.app_context():
            before = db.session.query(StatusSample).count()
            with patch("app.cron.record_status._check_db", return_value="ok"), \
                 patch("app.cron.record_status._check_redis", return_value="ok"), \
                 patch("app.cron.record_status._check_ollama", return_value="ok"), \
                 patch("app.cron.record_status._check_mistral", return_value="skipped"), \
                 patch("app.cron.record_status._check_web_self", return_value=("ok", 12)):
                result = run()
            after = db.session.query(StatusSample).count()
        assert result["recorded"] is True
        assert after == before + 1
        assert result["states"]["mistral"] == "skipped"

    def test_mistral_check_skipped_in_sovereign(self, monkeypatch):
        monkeypatch.setenv("SOVEREIGN_MODE", "true")
        from app.cron.record_status import _check_mistral

        # Sovereign mode short-circuits before any HTTP call.
        with patch("app.cron.record_status.requests.get") as mock_get:
            assert _check_mistral() == "skipped"
            mock_get.assert_not_called()

    def test_mistral_check_skipped_without_key(self, monkeypatch):
        monkeypatch.delenv("SOVEREIGN_MODE", raising=False)
        monkeypatch.setenv("MISTRAL_API_KEY", "")
        from app.cron.record_status import _check_mistral
        assert _check_mistral() == "skipped"

    def test_purge_drops_old_samples(self, app, db):
        from app.cron.record_status import _purge_old_samples
        from app.models.status_sample import StatusSample

        with app.app_context():
            old = StatusSample(
                sampled_at=datetime.now(timezone.utc) - timedelta(days=45),
            )
            recent = StatusSample(
                sampled_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )
            db.session.add_all([old, recent])
            db.session.commit()
            removed = _purge_old_samples(retention_days=30)
            remaining = db.session.query(StatusSample).count()
        assert removed >= 1
        assert remaining >= 1


# ─── archive_audit_log ────────────────────────────────────────────────

class TestArchiveAuditLog:
    def test_no_op_without_bucket(self, app, monkeypatch):
        monkeypatch.delenv("AUDIT_S3_BUCKET", raising=False)
        from app.cron.archive_audit_log import run
        with app.app_context():
            result = run()
        assert result["skipped"] is True
        assert result["archived"] == 0

    def test_serialise_round_trips_a_row(self, app, db):
        from app.cron.archive_audit_log import _serialise
        from app.models.audit_log import AuditLog

        with app.app_context():
            row = AuditLog(
                actor_user_id=1,
                actor_email="admin@example.com",
                action="test.archive",
                target_type="org",
                target_id="42",
                payload_json='{"k":"v"}',
                ip_address="127.0.0.1",
                user_agent="curl/8",
                created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
            )
            data = _serialise(row)
        assert data["action"] == "test.archive"
        assert data["actor_email"] == "admin@example.com"
        assert data["payload_json"] == '{"k":"v"}'
        assert data["created_at"].startswith("2026-01-01T12:00:00")

    def test_s3_client_returns_none_without_bucket(self, monkeypatch):
        monkeypatch.delenv("AUDIT_S3_BUCKET", raising=False)
        from app.cron.archive_audit_log import _s3_client
        client, bucket = _s3_client()
        assert client is None
        assert bucket is None


# ─── backup_databases ──────────────────────────────────────────────────

class TestBackupDatabases:
    def test_rotate_drops_old_directories(self, app, tmp_path, monkeypatch):
        from app.cron import backup_databases

        # Point the module at a temp backup dir.
        monkeypatch.setattr(backup_databases, "_BACKUP_DIR", tmp_path)
        monkeypatch.setattr(backup_databases, "_RETENTION_DAYS", 7)

        # 8-day-old dir → should go.
        old = tmp_path / (datetime.now(timezone.utc) - timedelta(days=8)).date().isoformat()
        old.mkdir()
        # 2-day-old dir → keep.
        recent = tmp_path / (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
        recent.mkdir()
        # Garbage entry (not a date dir) → keep.
        (tmp_path / "README").write_text("hi")

        with app.app_context():
            n = backup_databases._rotate_old_backups()
        assert n == 1
        assert not old.exists()
        assert recent.exists()

    def test_chroma_backup_skipped_when_dir_missing(self, app, tmp_path, monkeypatch):
        from app.cron import backup_databases
        monkeypatch.setattr(backup_databases, "_CHROMA_DIR", tmp_path / "nope")
        target = tmp_path / "out.tar.gz"
        with app.app_context():
            assert backup_databases._backup_chroma(target) is True
        assert not target.exists()  # nothing written, but no error

    def test_chroma_backup_writes_tarball(self, app, tmp_path, monkeypatch):
        from app.cron import backup_databases
        chroma_dir = tmp_path / "chroma_db"
        chroma_dir.mkdir()
        (chroma_dir / "meta.json").write_text('{"k":"v"}')
        monkeypatch.setattr(backup_databases, "_CHROMA_DIR", chroma_dir)
        target = tmp_path / "out.tar.gz"
        with app.app_context():
            assert backup_databases._backup_chroma(target) is True
        assert target.exists()
        assert target.stat().st_size > 0

    def test_offhost_skipped_without_remote(self, app, monkeypatch, tmp_path):
        monkeypatch.delenv("BACKUP_REMOTE", raising=False)
        from app.cron.backup_databases import _push_offhost
        with app.app_context():
            result = _push_offhost(tmp_path, "2026-01-01")
        assert result == "skipped"

    def test_offhost_calls_rclone_when_remote_set(self, app, monkeypatch, tmp_path):
        monkeypatch.setenv("BACKUP_REMOTE", "gdrive:test")
        from app.cron.backup_databases import _push_offhost

        fake_run = MagicMock()
        fake_run.return_value = MagicMock(returncode=0, stderr=b"")
        with app.app_context():
            with patch("app.cron.backup_databases.subprocess.run", fake_run):
                result = _push_offhost(tmp_path, "2026-01-01")
        assert result == "ok"
        cmd = fake_run.call_args.args[0]
        assert cmd[0] == "rclone"
        assert "gdrive:test/2026-01-01/" in cmd

    def test_offhost_reports_error_on_rclone_failure(self, app, monkeypatch, tmp_path):
        monkeypatch.setenv("BACKUP_REMOTE", "gdrive:test")
        from app.cron.backup_databases import _push_offhost

        fake_run = MagicMock()
        fake_run.return_value = MagicMock(returncode=1, stderr=b"network down")
        with app.app_context():
            with patch("app.cron.backup_databases.subprocess.run", fake_run):
                result = _push_offhost(tmp_path, "2026-01-01")
        assert result.startswith("error")
