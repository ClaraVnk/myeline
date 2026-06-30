#!/usr/bin/env python
"""
Complete RGPD Data Cleanup Script
GDPR Article 5 - Storage Limitation

This script runs all cleanup tasks:
- Delete unconfirmed accounts after 30 days
- Anonymize inactive accounts after 3 years
- Delete old log files (6 months for general, 1 year for security)

Usage:
    python app/cron/cleanup_rgpd.py

Crontab example (run daily at 2:00 AM):
    0 2 * * * cd /var/www/myeline && python app/cron/cleanup_rgpd.py >> /var/log/myeline/cleanup.log 2>&1
"""

import os
import sys
from datetime import datetime

from app.utils.time_utils import utcnow


# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from app import create_app  # noqa: E402
from app.ext import db  # noqa: E402
from app.models.user import User  # noqa: E402
from app.utils.logger import get_logger  # noqa: E402


logger = get_logger(__name__)

CRON_SCHEDULE = "0 2 * * 0"
CRON_DESCRIPTION = "Weekly GDPR cleanup: delete unconfirmed accounts > 30 days"


def print_separator(title):
    """Print a formatted separator."""
    print("\n" + "=" * 50)
    print(title)
    print("=" * 50)


def show_statistics_before():
    """Show statistics before cleanup."""
    print_separator("📊 Statistics before cleanup")

    app = create_app()
    with app.app_context():
        from datetime import timedelta

        cutoff_30_days = utcnow() - timedelta(days=30)
        cutoff_3_years = utcnow() - timedelta(days=3 * 365)

        # Unconfirmed accounts
        unconfirmed = User.query.filter(
            User.is_confirmed.is_(False), User.created_at < cutoff_30_days
        ).count()

        # Inactive accounts
        inactive = User.query.filter(
            User.is_confirmed.is_(True),
            User.is_active.is_(True),
            db.or_(User.last_login.is_(None), User.last_login < cutoff_3_years),
        ).count()

        print(f"Unconfirmed accounts (>30 days): {unconfirmed}")
        print(f"Inactive accounts (>3 years): {inactive}")

        # Count log files
        logs_dir = os.path.join(os.getcwd(), "logs")
        if os.path.exists(logs_dir):
            log_count = sum(1 for e in os.scandir(logs_dir) if e.name.endswith(".log") and e.is_file())
            print(f"Log files: {log_count}")

        # Count upload files
        uploads_dir = os.path.join(os.getcwd(), "uploads")
        if os.path.exists(uploads_dir):
            upload_count = sum(1 for e in os.scandir(uploads_dir) if e.is_file())
            print(f"Upload files: {upload_count}")


# Bug CC5 — les fonctions de delete vivaient dupliquées entre les deux
# modules `cleanup_rgpd` et `data_retention_rgpd` avec le même bug (pas
# de scrubbing audit_log). On réutilise désormais celles de
# `data_retention_rgpd` qui passent par le helper GDPR.
from app.cron.data_retention_rgpd import anonymize_inactive_accounts, cleanup_unconfirmed_accounts  # noqa: E402


def cleanup_old_logs():
    """Delete log files older than 6 months (except payment logs)."""
    print_separator("4. Deleting old general logs (>6 months)")

    from datetime import timedelta

    logs_dir = os.path.join(os.getcwd(), "logs")
    if not os.path.exists(logs_dir):
        print("✅ No logs directory found.")
        return True

    # Bug L2 (audit round 2) — `datetime.now()` (naive, TZ locale) vs
    # `datetime.fromtimestamp(st_mtime)` (naive aussi mais epoch) :
    # OK fonctionnellement mais incohérent avec le reste du projet qui
    # utilise utcnow(). On aligne en TZ-aware UTC.
    from datetime import timezone as _tz
    cutoff_date = utcnow() - timedelta(days=180)  # 6 months
    deleted_count = 0

    for entry in os.scandir(logs_dir):
        if not entry.is_file() or not entry.name.endswith(".log"):
            continue
        if "payment" in entry.name.lower():
            continue
        try:
            mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=_tz.utc)
            if mtime < cutoff_date:
                os.remove(entry.path)
                logger.info("Deleted old log file: %s", entry.name)
                deleted_count += 1
        except Exception as e:
            logger.error("Error deleting log file %s: %s", entry.name, e)

    print(f"✅ Deleted {deleted_count} old log file(s)")
    logger.info("Successfully deleted %d old log files", deleted_count)
    return True


def show_statistics_after():
    """Show statistics after cleanup."""
    print_separator("📊 Statistics after cleanup")

    app = create_app()
    with app.app_context():
        from datetime import timedelta

        cutoff_30_days = utcnow() - timedelta(days=30)
        cutoff_3_years = utcnow() - timedelta(days=3 * 365)

        # Unconfirmed accounts
        unconfirmed = User.query.filter(
            User.is_confirmed.is_(False), User.created_at < cutoff_30_days
        ).count()

        # Inactive accounts
        inactive = User.query.filter(
            User.is_confirmed.is_(True),
            User.is_active.is_(True),
            db.or_(User.last_login.is_(None), User.last_login < cutoff_3_years),
        ).count()

        print(f"Unconfirmed accounts (>30 days): {unconfirmed}")
        print(f"Inactive accounts (>3 years): {inactive}")


def main():
    """Main execution function."""
    print_separator(f"Complete RGPD Cleanup Script - {datetime.now()}")

    logger.info("Starting complete RGPD cleanup")

    # Show statistics before
    show_statistics_before()

    # Run all cleanup tasks
    success = True
    success = cleanup_unconfirmed_accounts() and success
    success = anonymize_inactive_accounts() and success
    success = cleanup_old_logs() and success

    # Show statistics after
    show_statistics_after()

    # Log completion
    print_separator(f"Complete RGPD Cleanup Script Completed - {datetime.now()}")

    if success:
        logger.info("Complete RGPD cleanup finished successfully")
        return 0
    else:
        logger.error("Complete RGPD cleanup finished with errors")
        return 1


if __name__ == "__main__":
    sys.exit(main())
