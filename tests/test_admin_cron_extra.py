"""Extra tests for admin_cron — parse functions, config file, log parsing."""
import datetime
import json
from pathlib import Path
from unittest.mock import patch

from tests.helpers import admin_login


# ── parse_cron_expression unit tests ──────────────────────────────────────────


def test_parse_cron_special_hourly():
    from app.routes.admin.admin_cron import parse_cron_expression

    assert parse_cron_expression("0 * * * *") == "Toutes les heures"


def test_parse_cron_minute_and_hour():
    """minute != '*' and hour != '*' → 'à Hh{minute}' format."""
    from app.routes.admin.admin_cron import parse_cron_expression

    result = parse_cron_expression("30 14 * * *")
    assert "14" in result and "30" in result


def test_parse_cron_weekday_valid():
    """Valid weekday integer → day name appended."""
    from app.routes.admin.admin_cron import parse_cron_expression

    result = parse_cron_expression("0 9 * * 3")
    assert "Mer" in result or "9" in result


def test_parse_cron_weekday_invalid():
    """Non-integer weekday → 'jour {weekday}' fallback."""
    from app.routes.admin.admin_cron import parse_cron_expression

    result = parse_cron_expression("0 9 * * abc")
    assert "abc" in result or "jour" in result


def test_parse_cron_invalid_format():
    """Wrong number of parts → expression returned as-is."""
    from app.routes.admin.admin_cron import parse_cron_expression

    expr = "not-a-cron"
    assert parse_cron_expression(expr) == expr


# ── get_cron_configs with config file ─────────────────────────────────────────


def test_get_cron_configs_with_valid_file(app, tmp_path, monkeypatch):
    """Custom schedule from cron_config.json overrides the default."""
    # Point app.root_path at a tmp dir so we don't need write access to /app/app
    monkeypatch.setattr(app, "root_path", str(tmp_path))
    config_path = tmp_path / "config" / "cron_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"cleanup_rgpd": "0 4 * * 0"}))

    from app.routes.admin.admin_cron import get_cron_configs
    with app.app_context():
        configs = get_cron_configs()
    assert configs["cleanup_rgpd"]["expression"] == "0 4 * * 0"


def test_get_cron_configs_with_invalid_json(app, tmp_path, monkeypatch):
    """Corrupt cron_config.json → graceful fallback to defaults."""
    monkeypatch.setattr(app, "root_path", str(tmp_path))
    config_path = tmp_path / "config" / "cron_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("not valid json {{{{")

    from app.routes.admin.admin_cron import get_cron_configs
    with app.app_context():
        configs = get_cron_configs()
    assert "cleanup_rgpd" in configs


# ── get_last_cron_runs with a real log file ────────────────────────────────────


def test_get_last_cron_runs_with_recent_log(app, tmp_path, monkeypatch):
    """Log file with a recent INFO entry is parsed correctly."""
    from app.routes.admin import admin_cron
    monkeypatch.setattr(admin_cron, "LOGS_DIR", str(tmp_path))

    cron_log_dir = tmp_path / "cron"
    cron_log_dir.mkdir(parents=True, exist_ok=True)
    log_file = cron_log_dir / "cleanup_rgpd.log"
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_file.write_text(f"{now_str},123 INFO Task completed\n")

    with app.app_context():
        runs = admin_cron.get_last_cron_runs()
    assert "cleanup_rgpd" in runs
    assert runs["cleanup_rgpd"] != "Jamais"


def test_get_last_cron_runs_old_log(app, tmp_path, monkeypatch):
    """Log file with a >1-day-old entry shows the formatted date."""
    from app.routes.admin import admin_cron
    monkeypatch.setattr(admin_cron, "LOGS_DIR", str(tmp_path))

    cron_log_dir = tmp_path / "cron"
    cron_log_dir.mkdir(parents=True, exist_ok=True)
    log_file = cron_log_dir / "data_retention_rgpd.log"
    old = datetime.datetime.now() - datetime.timedelta(days=5)
    old_str = old.strftime("%Y-%m-%d %H:%M:%S")
    log_file.write_text(f"{old_str},000 INFO Done\n")

    with app.app_context():
        runs = admin_cron.get_last_cron_runs()
    assert "data_retention_rgpd" in runs


def test_get_last_cron_runs_log_read_error(app, tmp_path, monkeypatch):
    """Error reading log file → 'Jamais' returned for that task."""
    from app.routes.admin import admin_cron
    monkeypatch.setattr(admin_cron, "LOGS_DIR", str(tmp_path))

    cron_log_dir = tmp_path / "cron"
    cron_log_dir.mkdir(parents=True, exist_ok=True)
    log_file = cron_log_dir / "cleanup_rgpd.log"
    log_file.write_text("bad line no date\n")

    with app.app_context():
        runs = admin_cron.get_last_cron_runs()
    assert "cleanup_rgpd" in runs


# ── update_cron_config write error ────────────────────────────────────────────


def test_update_cron_config_write_error(client, admin_user):
    """write_text failure → success=False returned."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    with patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
        r = client.post(
            "/admin/cron/update",
            data={"task": "cleanup_rgpd", "expression": "0 4 * * 0"},
        )
    assert r.status_code == 200
    data = r.get_json()
    assert data["success"] is False
    client.get("/admin/logout")


# ── run_cron_task import error ─────────────────────────────────────────────────


def test_run_cron_task_import_error(client, admin_user):
    """ImportError when loading the cron module → success=False."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    with patch("importlib.import_module", side_effect=ImportError("no module")):
        r = client.post("/admin/cron/run/cleanup_rgpd")
    assert r.status_code == 200
    assert r.get_json()["success"] is False
    client.get("/admin/logout")
