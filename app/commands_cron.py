"""Unified cron CLI: `flask run-cron <task>` dispatches to the right module.

Wraps the existing per-module cron functions behind a single command so
the supercronic sidecar can call them by name without each module
needing to register its own Click command.
"""
import importlib

import click
from flask.cli import with_appcontext


# Mirror of admin_cron._CRON_TASK_MAP — duplicated here to keep this CLI
# module decoupled from the admin blueprint (the sidecar imports nothing
# from admin/). Adding a task means appending one line in BOTH places.
_TASKS = {
    "remind_unconfirmed_users": ("app.cron.remind_unconfirmed_users", "remind_unconfirmed_users"),
    "cleanup_rgpd": ("app.cron.cleanup_rgpd", "main"),
    "data_retention_rgpd": ("app.cron.data_retention_rgpd", "main"),
    "send_recap_digests": ("app.cron.send_recap_digests", "send_recap_digests"),
    "check_user_scrapers": ("app.cron.check_user_scrapers", "check_user_scrapers"),
    "check_cloud_sync": ("app.cron.check_cloud_sync", "check_cloud_sync"),
    "check_watch_alerts": ("app.cron.check_watch_alerts", "check_watch_alerts"),
    "backup_databases": ("app.cron.backup_databases", "run"),
    "record_status": ("app.cron.record_status", "run"),
    "archive_audit_log": ("app.cron.archive_audit_log", "run"),
}


@click.command("run-cron")
@click.argument("task")
@with_appcontext
def run_cron(task):
    """Execute a registered cron task by name.

    Used by the supercronic sidecar — one `flask run-cron <task>` per
    crontab line. The Flask AppGroup-wrapped commands inside individual
    modules are unwrapped here (we want the underlying callable, not
    Click's wrapper).
    """
    if task not in _TASKS:
        raise click.UsageError(f"Unknown cron task: {task}. Known: {sorted(_TASKS)}")
    module_path, func_name = _TASKS[task]
    module = importlib.import_module(module_path)
    raw = getattr(module, func_name)
    # If it was decorated with @cron_cli.command (Click), unwrap it.
    func = getattr(raw, "callback", raw)
    click.echo(f"▶ {task}")
    result = func()
    if result is not None:
        click.echo(f"  → {result}")


def init_app(app):
    app.cli.add_command(run_cron)
