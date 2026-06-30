"""Flask CLI commands for data retention and GDPR compliance.

This module implements automated data retention policies:
- Delete unconfirmed accounts after 30 days (GDPR Article 5)
- Anonymize inactive accounts after 3 years (GDPR Article 5)
"""

from datetime import timedelta

import click
from flask.cli import with_appcontext

from app.ext import db
from app.models.user import User
from app.utils.logger import get_logger
from app.utils.time_utils import utcnow


logger = get_logger("commands", subfolder="cli")


@click.command("cleanup-unconfirmed-accounts")
@with_appcontext
def cleanup_unconfirmed_accounts():
    """Delete user accounts that have not been confirmed within 30 days.

    GDPR Article 5 - Storage limitation principle.
    Unconfirmed accounts are considered abandoned and deleted automatically.

    Usage:
        flask cleanup-unconfirmed-accounts
    """
    logger.info("Starting cleanup of unconfirmed accounts (30 days)")

    # Calculate cutoff date (30 days ago)
    cutoff_date = utcnow() - timedelta(days=30)

    # Find unconfirmed accounts older than 30 days
    unconfirmed_users = User.query.filter(
        User.is_confirmed.is_(False), User.created_at < cutoff_date
    ).all()

    if not unconfirmed_users:
        logger.info("No unconfirmed accounts to delete")
        click.echo("✅ No unconfirmed accounts older than 30 days found.")
        return

    deleted_count = 0
    for user in unconfirmed_users:
        try:
            logger.info("Deleting unconfirmed account user_id=%s (created %s)", user.id, user.created_at)
            db.session.delete(user)
            deleted_count += 1
        except Exception as e:
            logger.error("Error deleting user user_id=%s: %s", user.id, e)
            db.session.rollback()

    try:
        db.session.commit()
        logger.info(f"Successfully deleted {deleted_count} unconfirmed accounts")
        click.echo(
            f"✅ Deleted {deleted_count} unconfirmed account(s) older than 30 days."
        )
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error committing deletions: {e}")
        click.echo(f"❌ Error: {e}")


@click.command("anonymize-inactive-accounts")
@with_appcontext
def anonymize_inactive_accounts():
    """Delete user accounts that have been inactive for 3 years (RGPD Art. 17).

    Usage:
        flask anonymize-inactive-accounts
    """
    logger.info("Starting deletion of inactive accounts (3 years)")

    cutoff_date = utcnow() - timedelta(days=3 * 365)

    inactive_users = User.query.filter(
        User.is_confirmed.is_(True),
        User.is_active.is_(True),
        db.or_(User.last_login.is_(None), User.last_login < cutoff_date),
        User.created_at < cutoff_date,
    ).all()

    if not inactive_users:
        logger.info("No inactive accounts to delete")
        click.echo("✅ No inactive accounts older than 3 years found.")
        return

    deleted_count = 0
    for user in inactive_users:
        try:
            logger.info(
                "Deleting inactive account user_id=%s (last login: %s)",
                user.id,
                user.last_login or user.created_at,
            )
            db.session.delete(user)
            deleted_count += 1
        except Exception as e:
            logger.error("Error deleting inactive user_id=%s: %s", user.id, e)
            db.session.rollback()

    try:
        db.session.commit()
        logger.info("Successfully deleted %d inactive accounts", deleted_count)
        click.echo(f"✅ Deleted {deleted_count} inactive account(s) older than 3 years.")
    except Exception as e:
        db.session.rollback()
        logger.error("Error committing deletions: %s", e)
        click.echo(f"❌ Error: {e}")


@click.command("show-retention-stats")
@with_appcontext
def show_retention_stats():
    """Display statistics about data retention and accounts to be cleaned.

    Shows:
    - Number of unconfirmed accounts older than 30 days
    - Number of inactive accounts older than 3 years
    - Total accounts

    Usage:
        flask show-retention-stats
    """
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
        User.created_at < cutoff_3_years,
    ).count()

    # Total accounts
    total_count = User.query.count()
    confirmed_count = User.query.filter(User.is_confirmed.is_(True)).count()

    click.echo("\n📊 Data Retention Statistics")
    click.echo("=" * 50)
    click.echo(f"Total accounts: {total_count}")
    click.echo(f"Confirmed accounts: {confirmed_count}")
    click.echo(f"Unconfirmed accounts: {total_count - confirmed_count}")
    click.echo("\n🗑️  Accounts to be cleaned:")
    click.echo(f"  - Unconfirmed (>30 days): {unconfirmed_count}")
    click.echo(f"  - Inactive (>3 years): {inactive_count}")
    click.echo("=" * 50)

    if unconfirmed_count > 0:
        click.echo(
            f"\n💡 Run 'flask cleanup-unconfirmed-accounts' to delete {unconfirmed_count} account(s)"
        )
    if inactive_count > 0:
        click.echo(
            f"💡 Run 'flask anonymize-inactive-accounts' to anonymize {inactive_count} account(s)"
        )


def init_app(app):
    """Register CLI commands with Flask app.

    Args:
        app: Flask application instance
    """
    app.cli.add_command(cleanup_unconfirmed_accounts)
    app.cli.add_command(anonymize_inactive_accounts)
    app.cli.add_command(show_retention_stats)
