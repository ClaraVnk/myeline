#!/usr/bin/env python
"""
Data Retention Cron Script
GDPR Article 5 - Storage Limitation

This script should be run daily via cron to enforce data retention policies:
- Delete unconfirmed accounts after 30 days
- Anonymize inactive accounts after 3 years

Usage:
    python app/cron/data_retention_rgpd.py

Crontab example (run daily at 2:00 AM):
    0 2 * * * cd /var/www/myeline && python app/cron/data_retention_rgpd.py >> /var/log/myeline/retention.log 2>&1
"""

import os
import sys
from datetime import datetime, timedelta


# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from app import create_app  # noqa: E402
from app.ext import db  # noqa: E402
from app.models.user import User  # noqa: E402
from app.utils.logger import get_logger  # noqa: E402
from app.utils.time_utils import utcnow  # noqa: E402


logger = get_logger(__name__)

CRON_SCHEDULE = "0 3 * * 0"
CRON_DESCRIPTION = "Weekly GDPR retention: anonymise inactive accounts > 3 years"


def print_separator(title):
    """Print a formatted separator."""
    print("\n" + "=" * 50)
    print(title)
    print("=" * 50)


def show_retention_stats():
    """Show retention statistics."""
    print("\n📊 Statistics before cleanup:")

    app = create_app()
    with app.app_context():
        cutoff_30_days = utcnow() - timedelta(days=30)
        cutoff_3_years = utcnow() - timedelta(days=3 * 365)

        # Count unconfirmed accounts
        unconfirmed_count = User.query.filter(
            User.is_confirmed.is_(False), User.created_at < cutoff_30_days
        ).count()

        # Count inactive accounts
        inactive_count = User.query.filter(
            User.is_confirmed.is_(True),
            User.is_active.is_(True),
            db.or_(User.last_login.is_(None), User.last_login < cutoff_3_years),
        ).count()

        print(f"Unconfirmed accounts (>30 days): {unconfirmed_count}")
        print(f"Inactive accounts (>3 years): {inactive_count}")


def _purge_users(users, reason: str, label: str) -> bool:
    """Bug CC5 — scrub audit trail (placeholder déterministe sur
    actor_email) avant chaque delete, pour respecter Art 17 sur les
    audit_logs.
    """
    from app.utils.gdpr import scrub_user_audit_trail

    if not users:
        print(f"✅ No {label} accounts to delete.")
        logger.info("No %s accounts to delete", label)
        return True

    deleted_count = 0
    for user in users:
        try:
            logger.info("Deleting %s account user_id=%s", label, user.id)
            scrub_user_audit_trail(user, reason=reason)
            db.session.delete(user)
            deleted_count += 1
        except Exception as e:
            logger.error("Error deleting user user_id=%s: %s", user.id, e)

    try:
        db.session.commit()
        print(f"✅ Deleted {deleted_count} {label} account(s)")
        logger.info("Successfully deleted %d %s accounts", deleted_count, label)
        return True
    except Exception as e:
        db.session.rollback()
        logger.error("Error committing deletions: %s", e)
        print(f"❌ Error: {e}")
        return False


def cleanup_unconfirmed_accounts():
    """Delete unconfirmed accounts older than 30 days."""
    print("\n🗑️  Cleaning up unconfirmed accounts...")

    app = create_app()
    with app.app_context():
        cutoff_date = utcnow() - timedelta(days=30)

        unconfirmed_users = User.query.filter(
            User.is_confirmed.is_(False), User.created_at < cutoff_date
        ).all()
        return _purge_users(unconfirmed_users, reason="unconfirmed_cleanup", label="unconfirmed")


def anonymize_inactive_accounts():
    """Delete inactive accounts after 3 years (RGPD Art. 17 — real deletion)."""
    print("\n🗑️  Deleting inactive accounts...")

    app = create_app()
    with app.app_context():
        cutoff_date = utcnow() - timedelta(days=3 * 365)

        inactive_users = User.query.filter(
            User.is_confirmed.is_(True),
            User.is_active.is_(True),
            db.or_(User.last_login.is_(None), User.last_login < cutoff_date),
        ).all()
        return _purge_users(inactive_users, reason="retention_3y", label="inactive")


def main():
    """Main execution function."""
    print_separator(f"Data Retention Script - {datetime.now()}")

    logger.info("Starting data retention cleanup")

    # Show statistics before
    show_retention_stats()

    # Run cleanup tasks
    success = True
    success = cleanup_unconfirmed_accounts() and success
    success = anonymize_inactive_accounts() and success

    # Show statistics after
    print("\n📊 Statistics after cleanup:")
    show_retention_stats()

    # Log completion
    print_separator(f"Data Retention Script Completed - {datetime.now()}")

    if success:
        logger.info("Data retention cleanup finished successfully")
        return 0
    else:
        logger.error("Data retention cleanup finished with errors")
        return 1


if __name__ == "__main__":
    sys.exit(main())
