"""Additional cleanup commands for GDPR compliance.

This module implements additional automated cleanup policies:
- Rotate and clean old logs (GDPR Article 5)
"""

import os
from datetime import datetime, timedelta

import click
from flask.cli import with_appcontext

from app.utils.logger import get_logger
from app.utils.time_utils import utcnow


logger = get_logger("commands_cleanup", subfolder="cli")


@click.command("cleanup-old-logs")
@click.option(
    "--days",
    default=180,
    help="Delete logs older than N days (default: 180 = 6 months)",
)
@with_appcontext
def cleanup_old_logs(days):
    """Delete log files older than specified days.

    GDPR Article 5 - Storage limitation principle.
    Default: 6 months (180 days) for general logs.

    Usage:
        flask cleanup-old-logs
        flask cleanup-old-logs --days 365  # For security logs (1 year)
    """
    logger.info(f"Starting cleanup of logs older than {days} days")

    cutoff_date = utcnow() - timedelta(days=days)
    logs_folder = os.path.join(os.getcwd(), "logs")

    if not os.path.exists(logs_folder):
        click.echo("⚠️  Logs folder does not exist.")
        return

    deleted_count = 0
    total_size = 0

    # Exclude payment logs (10 years retention)
    exclude_patterns = ["payment", "stripe", "invoice", "billing"]

    for root, dirs, files in os.walk(logs_folder):
        for file in files:
            # Skip non-log files
            if not file.endswith((".log", ".log.gz", ".log.1", ".log.2")):
                continue

            # Skip payment-related logs
            if any(pattern in file.lower() for pattern in exclude_patterns):
                logger.info(f"Skipping payment log: {file}")
                continue

            file_path = os.path.join(root, file)

            try:
                # Get file modification time
                file_mtime = datetime.fromtimestamp(os.path.getmtime(file_path))

                if file_mtime < cutoff_date:
                    file_size = os.path.getsize(file_path)
                    os.remove(file_path)
                    deleted_count += 1
                    total_size += file_size
                    logger.info(f"Deleted old log: {file} (modified {file_mtime})")

            except Exception as e:
                logger.error(f"Error deleting log {file}: {e}")

    # Convert size to human-readable format
    size_mb = total_size / (1024 * 1024)

    logger.info(f"Successfully deleted {deleted_count} log files ({size_mb:.2f} MB)")
    click.echo(
        f"✅ Deleted {deleted_count} log file(s) older than {days} days ({size_mb:.2f} MB freed)."
    )


@click.command("show-cleanup-stats")
@with_appcontext
def show_cleanup_stats():
    """Display statistics about data to be cleaned.

    Shows:
    - Number of old log files

    Usage:
        flask show-cleanup-stats
    """
    cutoff_logs = utcnow() - timedelta(days=180)

    # Count old log files
    logs_folder = os.path.join(os.getcwd(), "logs")
    old_logs_count = 0
    old_logs_size = 0

    if os.path.exists(logs_folder):
        exclude_patterns = ["payment", "stripe", "invoice", "billing"]
        for root, dirs, files in os.walk(logs_folder):
            for file in files:
                if not file.endswith((".log", ".log.gz", ".log.1", ".log.2")):
                    continue
                if any(pattern in file.lower() for pattern in exclude_patterns):
                    continue

                file_path = os.path.join(root, file)
                try:
                    file_mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
                    if file_mtime < cutoff_logs:
                        old_logs_count += 1
                        old_logs_size += os.path.getsize(file_path)
                except (OSError, ValueError) as e:
                    logger.warning(f"Could not process log file {file}: {e}")
                    continue

    click.echo("\n📊 Cleanup Statistics")
    click.echo("=" * 50)
    click.echo(
        f"Old log files (>6 months): {old_logs_count} ({old_logs_size / (1024 * 1024):.2f} MB)"
    )
    click.echo("=" * 50)

    if old_logs_count > 0:
        click.echo(
            f"💡 Run 'flask cleanup-old-logs' to delete {old_logs_count} log file(s)"
        )


def init_app(app):
    """Register cleanup CLI commands with Flask app.

    Args:
        app: Flask application instance
    """
    app.cli.add_command(cleanup_old_logs)
    app.cli.add_command(show_cleanup_stats)
