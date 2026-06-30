"""Extra tests for admin_ops — RAG routes, tools_dashboard errors, file ops."""
import os
import time
from unittest.mock import MagicMock, patch

from tests.helpers import admin_login


# ── tools_dashboard with listdir errors ───────────────────────────────────────


def test_tools_dashboard_log_listdir_error(client, admin_user):
    """os.listdir failure for logs dir → empty log_files, still renders."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    with patch("app.routes.admin.admin_ops.os.listdir", side_effect=OSError("no dir")):
        r = client.get("/admin/tools")
    assert r.status_code == 200
    client.get("/admin/logout")


def test_tools_dashboard_report_listdir_error(client, admin_user):
    """Second os.listdir (reports) fails → empty report_files, still renders."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)

    call_count = [0]
    real_listdir = os.listdir

    def listdir_side_effect(path):
        call_count[0] += 1
        if call_count[0] == 2:
            raise OSError("reports dir missing")
        return real_listdir(path)

    with patch("app.routes.admin.admin_ops.os.listdir", side_effect=listdir_side_effect):
        r = client.get("/admin/tools")
    assert r.status_code == 200
    client.get("/admin/logout")


# ── clear_logs removes old files ──────────────────────────────────────────────


def test_clear_logs_removes_old_file(client, admin_user):
    """Files older than 30 days are removed."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)

    old_mtime = time.time() - (31 * 24 * 3600)

    with patch("app.routes.admin.admin_ops.os.listdir", return_value=["old.log"]), \
         patch("app.routes.admin.admin_ops.os.path.isfile", return_value=True), \
         patch("app.routes.admin.admin_ops.os.path.getmtime", return_value=old_mtime), \
         patch("app.routes.admin.admin_ops.os.remove") as mock_rm:
        r = client.post("/admin/tools/clear-logs")
    assert r.status_code == 200
    assert r.get_json()["success"] is True
    mock_rm.assert_called_once()
    client.get("/admin/logout")


def test_clear_logs_listdir_error(client, admin_user):
    """os.listdir failure → success=False."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    with patch("app.routes.admin.admin_ops.os.listdir", side_effect=OSError("no dir")):
        r = client.post("/admin/tools/clear-logs")
    assert r.status_code == 200
    assert r.get_json()["success"] is False
    client.get("/admin/logout")


# ── restart_app exception ─────────────────────────────────────────────────────


def test_restart_app_popen_error(client, admin_user):
    """subprocess.Popen failure → success=False."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    with patch("subprocess.Popen", side_effect=OSError("docker not found")):
        r = client.post("/admin/tools/restart")
    assert r.status_code == 200
    assert r.get_json()["success"] is False
    client.get("/admin/logout")


# ── delete_log with existing file ─────────────────────────────────────────────


def test_delete_log_existing_file(client, admin_user, tmp_path, monkeypatch):
    """Existing log file is deleted successfully."""
    # Redirect LOGS_DIR to a tmp path so we don't need write access to /app/logs
    from app.routes.admin import admin_ops
    monkeypatch.setattr(admin_ops, "LOGS_DIR", str(tmp_path))

    log_path = tmp_path / "test_tmp.log"
    log_path.write_text("test\n")

    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.delete("/admin/tools/delete-log/test_tmp.log")
    assert r.status_code == 200
    assert r.get_json()["success"] is True
    client.get("/admin/logout")


def test_delete_log_permission_error(client, admin_user):
    """os.remove failure → success=False."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    with patch("os.path.exists", return_value=True), \
         patch("os.path.isfile", return_value=True), \
         patch("os.remove", side_effect=PermissionError("denied")):
        r = client.delete("/admin/tools/delete-log/locked.log")
    assert r.status_code == 200
    assert r.get_json()["success"] is False
    client.get("/admin/logout")


# ── delete_report with existing file ──────────────────────────────────────────


def test_delete_report_existing_file(client, admin_user, tmp_path, monkeypatch):
    """Existing report file is deleted successfully."""
    monkeypatch.setattr("app.routes.admin.admin_ops.REPORTS_DIR", str(tmp_path))

    report_path = os.path.join(str(tmp_path), "test_tmp.json")
    with open(report_path, "w") as f:
        f.write("{}\n")

    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.delete("/admin/tools/delete-report/test_tmp.json")
    assert r.status_code == 200
    assert r.get_json()["success"] is True
    client.get("/admin/logout")


def test_delete_report_permission_error(client, admin_user):
    """os.remove failure on report → success=False."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    with patch("os.path.exists", return_value=True), \
         patch("os.path.isfile", return_value=True), \
         patch("os.remove", side_effect=PermissionError("denied")):
        r = client.delete("/admin/tools/delete-report/locked.json")
    assert r.status_code == 200
    assert r.get_json()["success"] is False
    client.get("/admin/logout")


# ── Path-traversal guard on delete_log / delete_report (audit round 4, A4-C2) ─


def test_delete_log_refuses_path_traversal(client, admin_user, tmp_path, monkeypatch):
    """Werkzeug already rejects /-containing URL fragments at the routing
    layer (404), but our app-level guard refuses any remaining escape paths
    with 400. Accept either as a valid defence outcome."""
    from app.routes.admin import admin_ops
    monkeypatch.setattr(admin_ops, "LOGS_DIR", str(tmp_path))

    outside = tmp_path.parent / "secret.txt"
    outside.write_text("must not be removed\n")

    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.delete("/admin/tools/delete-log/..%2Fsecret.txt")
    assert r.status_code in (400, 404)
    assert outside.exists()  # untouched
    client.get("/admin/logout")


def test_delete_log_refuses_dotdot_filename(client, admin_user, tmp_path, monkeypatch):
    """A bare '..' passes Werkzeug but must be rejected by our guard."""
    from app.routes.admin import admin_ops
    monkeypatch.setattr(admin_ops, "LOGS_DIR", str(tmp_path))

    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.delete("/admin/tools/delete-log/..")
    assert r.status_code == 400
    assert r.get_json()["success"] is False
    client.get("/admin/logout")


def test_delete_log_refuses_nonregex_filename(client, admin_user, tmp_path, monkeypatch):
    """A filename with characters outside the safe regex (space, $, etc.)
    is rejected even though Werkzeug routes it."""
    from app.routes.admin import admin_ops
    monkeypatch.setattr(admin_ops, "LOGS_DIR", str(tmp_path))

    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.delete("/admin/tools/delete-log/bad%20name%24.log")
    assert r.status_code == 400
    client.get("/admin/logout")


def test_delete_report_refuses_path_traversal(client, admin_user, tmp_path, monkeypatch):
    from app.routes.admin import admin_ops
    monkeypatch.setattr(admin_ops, "REPORTS_DIR", str(tmp_path))

    outside = tmp_path.parent / "secret-report.json"
    outside.write_text("{}\n")

    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    r = client.delete("/admin/tools/delete-report/..%2Fsecret-report.json")
    assert r.status_code in (400, 404)
    assert outside.exists()
    client.get("/admin/logout")


# ── RAG routes ─────────────────────────────────────────────────────────────────


def test_rag_stats(client, admin_user):
    """GET /admin/rag/stats returns JSON stats."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    with patch("app.services.rag_service.get_index_stats", return_value={"count": 0}):
        r = client.get("/admin/rag/stats")
    assert r.status_code == 200
    assert r.get_json() == {"count": 0}
    client.get("/admin/logout")


def test_rag_index_all(client, admin_user):
    """POST /admin/rag/index-all starts background thread."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    with patch("app.services.rag_service.index_all_newsletters"), \
         patch("threading.Thread") as mock_thread:
        mock_thread.return_value.start.return_value = None
        r = client.post("/admin/rag/index-all")
    assert r.status_code == 200
    assert r.get_json()["success"] is True
    client.get("/admin/logout")


def test_rag_reindex(client, admin_user):
    """POST /admin/rag/reindex starts full reindex thread."""
    with patch("app.utils.mailer.send_email_via_brevo_api"):
        admin_login(client)
    with patch("app.services.rag_service.index_all_newsletters"), \
         patch("threading.Thread") as mock_thread:
        mock_thread.return_value.start.return_value = None
        r = client.post("/admin/rag/reindex")
    assert r.status_code == 200
    assert r.get_json()["success"] is True
    client.get("/admin/logout")
