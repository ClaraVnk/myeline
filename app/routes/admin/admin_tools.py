from pathlib import Path

from flask import Blueprint, flash, redirect, render_template, url_for
from flask_login import login_required
from werkzeug.utils import secure_filename

from app.ext import db
from app.routes.admin.admin import admin_bp, admin_required
from app.services.admin_services import read_file_if_safe
from app.utils.logger import get_logger


logger = get_logger(__name__)

admin_tools_bp = Blueprint("admin_tools", __name__)


@admin_bp.route("/view_log/<filename>")
@login_required
@admin_required
def view_log(filename):
    filename = secure_filename(filename)
    try:
        # Déterminer catégorie à partir du nom du fichier (ex: "workers_error.log" -> "workers")
        category = filename.split("_")[0]
        base_logs_dir = Path(__file__).resolve().parent.parent.parent / "logs"
        logs_dir = base_logs_dir / category
        content = read_file_if_safe(filename, logs_dir)
        logger.info(f"Fichier log affiché : {filename} dans {logs_dir}")
        return render_template(
            "admin/view_file.html",
            filename=filename,
            content=content,
            type="log",
        )
    except PermissionError:
        logger.error("Fichier non autorisé")
        flash("Fichier non autorisé", "danger")
        return redirect(url_for("admin.tools_dashboard"))
    except Exception as e:
        logger.error(f"Erreur de lecture du fichier {filename} : {e}")
        flash("Erreur de lecture du fichier", "danger")
        return redirect(url_for("admin.tools_dashboard"))


@admin_bp.route("/view_report/<filename>")
@login_required
@admin_required
def view_report(filename):
    filename = secure_filename(filename)
    try:
        # Déterminer catégorie à partir du nom du fichier (ex: "scrappers_report.json" -> "scrappers")
        category = filename.split("_")[0]
        base_reports_dir = (
            Path(__file__).resolve().parent.parent.parent / "data" / "raw"
        )
        reports_dir = base_reports_dir / category
        content = read_file_if_safe(filename, reports_dir)
        logger.info(f"Fichier report affiché : {filename} dans {reports_dir}")
        return render_template(
            "admin/view_file.html",
            filename=filename,
            content=content,
            type="report",
        )
    except PermissionError:
        logger.error("Fichier non autorisé")
        flash("Fichier non autorisé", "danger")
        return redirect(url_for("admin.tools_dashboard"))
    except Exception as e:
        logger.error(f"Erreur de lecture du fichier {filename} : {e}")
        flash("Erreur de lecture du fichier", "danger")
        return redirect(url_for("admin.tools_dashboard"))


@admin_bp.route("/audit")
@login_required
@admin_required
def audit_log():
    """Browse the append-only AuditLog with filters.

    Filterable by action prefix (e.g. "organization.*"), actor email,
    target_type+target_id, and date range. Pagination at 100/page.
    """
    from datetime import datetime, timedelta

    from flask import request as _req

    from app.models.audit_log import AuditLog

    page = max(1, _req.args.get("page", 1, type=int))
    per_page = 100

    q = AuditLog.query
    # Bug L4 (audit round 2) — cap toutes les valeurs filter pour éviter
    # qu'un admin (compromis ou distrait) tape une chaîne géante qui
    # ferait un LIKE coûteux ou saturerait les logs.
    action_filter = (_req.args.get("action") or "").strip()[:80]
    if action_filter:
        # Support "prefix.*" — wildcard suffix only.
        if action_filter.endswith(".*"):
            q = q.filter(AuditLog.action.like(action_filter[:-2] + ".%"))
        else:
            q = q.filter(AuditLog.action == action_filter)
    actor = (_req.args.get("actor") or "").strip()[:120]
    if actor:
        # Bug L5 — l'ancien code utilisait actor brut dans un LIKE,
        # ce qui faisait que `%` et `_` saisis par l'admin agissaient
        # comme wildcards (résultats trompeurs même si SQLAlchemy bind
        # la valeur — donc pas une injection, juste un faux résultat).
        # Escape les méta-caractères LIKE.
        actor_escaped = actor.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        q = q.filter(AuditLog.actor_email.like(f"%{actor_escaped}%", escape="\\"))
    target_type = (_req.args.get("target_type") or "").strip()[:40]
    if target_type:
        q = q.filter(AuditLog.target_type == target_type)
    target_id = (_req.args.get("target_id") or "").strip()[:80]
    if target_id:
        q = q.filter(AuditLog.target_id == target_id)
    days = _req.args.get("days", 30, type=int)
    if days > 0:
        # datetime.utcnow() is deprecated in 3.12; the project standard
        # is the timezone-aware utcnow() helper.
        from app.utils.time_utils import utcnow as _utcnow
        q = q.filter(AuditLog.created_at >= _utcnow() - timedelta(days=days))

    total = q.count()
    rows = (
        q.order_by(AuditLog.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    # Distinct action names for the filter dropdown — capped because
    # in the long run this list should grow only slowly (one entry per
    # audited action symbol, not per row).
    actions = (
        db.session.query(AuditLog.action)
        .distinct()
        .order_by(AuditLog.action)
        .limit(200)
        .all()
    )
    actions = [a[0] for a in actions]

    return render_template(
        "admin/audit_log.html",
        rows=rows,
        actions=actions,
        action_filter=action_filter,
        actor_filter=actor,
        target_type_filter=target_type,
        target_id_filter=target_id,
        days=days,
        page=page,
        per_page=per_page,
        total=total,
    )


# ─── Cloud connectors admin (BYOC for on-prem) ────────────────────────

@admin_bp.route("/cloud-providers")
@login_required
@admin_required
def cloud_providers():
    """Show the OAuth redirect URIs the operator must whitelist in their
    own developer consoles, and surface which providers are currently
    configured (env vars present) vs. dormant (env vars empty).

    Useful primarily on hybrid (sovereign-hybride) installs where the
    operator needs to register their own OAuth apps with Google /
    Microsoft / Dropbox. Classic instances see it too — it's still
    useful as a "current connector status" panel.
    """
    import os as _os

    from flask import current_app, render_template

    from app.utils.deployment_mode import is_sovereign

    base = current_app.config.get("EXTERNAL_URL", "").rstrip("/") \
        or current_app.config.get("OAUTH_REDIRECT_BASE_URL", "").rstrip("/")

    # Sovereign mode: public-cloud OAuth providers are gated off everywhere
    # else in the app — showing their redirect URIs here would just confuse
    # the operator. Only S3 (toward an internal MinIO) and WebDAV (toward an
    # internal Nextcloud) are sane in air-gap, and those don't need OAuth
    # configuration at all (the user enters credentials per-connection).
    if is_sovereign():
        return render_template(
            "admin/cloud_providers.html",
            providers=[],
            external_url=base,
        )

    providers = [
        {
            "key": "gdrive",
            "label": "Google Drive",
            "console_url": "https://console.cloud.google.com/iam-admin/serviceaccounts",
            "redirect_uri": None,
            "env_id": "GOOGLE_SERVICE_ACCOUNT_CREDENTIALS",
            "env_secret": None,
            "configured": bool(_os.getenv("GOOGLE_SERVICE_ACCOUNT_CREDENTIALS")),
        },
        {
            "key": "onedrive",
            "label": "OneDrive (Microsoft Graph)",
            "console_url": "https://portal.azure.com",
            "redirect_uri": f"{base}/user/cloud/onedrive/callback",
            "env_id": "ONEDRIVE_CLIENT_ID",
            "env_secret": "ONEDRIVE_CLIENT_SECRET",
            "configured": bool(_os.getenv("ONEDRIVE_CLIENT_ID")
                               and _os.getenv("ONEDRIVE_CLIENT_SECRET")),
        },
        {
            "key": "dropbox",
            "label": "Dropbox",
            "console_url": "https://www.dropbox.com/developers/apps",
            "redirect_uri": f"{base}/user/cloud/dropbox/callback",
            "env_id": "DROPBOX_APP_KEY",
            "env_secret": "DROPBOX_APP_SECRET",
            "configured": bool(_os.getenv("DROPBOX_APP_KEY")
                               and _os.getenv("DROPBOX_APP_SECRET")),
        },
        {
            "key": "kdrive",
            "label": "kDrive Infomaniak (OAuth)",
            "console_url": "https://developer.infomaniak.com",
            "redirect_uri": f"{base}/user/cloud/kdrive/callback",
            "env_id": "KDRIVE_CLIENT_ID",
            "env_secret": "KDRIVE_CLIENT_SECRET",
            "configured": bool(_os.getenv("KDRIVE_CLIENT_ID")
                               and _os.getenv("KDRIVE_CLIENT_SECRET")),
        },
    ]
    return render_template(
        "admin/cloud_providers.html",
        providers=providers,
        external_url=base,
    )
