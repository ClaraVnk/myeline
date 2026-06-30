import importlib
import json
import os
import threading
from datetime import datetime
from pathlib import Path

from flask import current_app, jsonify, request
from flask_login import login_required

from app.routes.admin.admin import admin_bp, admin_required
from app.utils.logger import get_logger


logger = get_logger("admin_cron", subfolder="routes")

LOGS_DIR = os.path.join(os.path.dirname(__file__), "../../../logs")

_CRON_DEFAULTS = {
    "remind_unconfirmed_users": "0 10 * * *",
    "cleanup_rgpd": "0 2 * * 0",
    "data_retention_rgpd": "0 3 * * 0",
    "send_recap_digests": "0 * * * *",
    "check_user_scrapers": "0 */6 * * *",
    "check_cloud_sync": "0 * * * *",
    "check_watch_alerts": "0 8,20 * * *",
}

_CRON_META = {
    "remind_unconfirmed_users": (
        "📧 Rappel comptes non confirmés",
        "Rappelle aux utilisateurs de confirmer leur compte",
        "CRON_UNCONFIRMED_REMIND",
    ),
    "cleanup_rgpd": ("🧹 Nettoyage RGPD", "Supprime les données expirées (RGPD)", "CRON_CLEANUP_RGPD"),
    "data_retention_rgpd": ("📦 Rétention données RGPD", "Anonymise les comptes inactifs", "CRON_DATA_RETENTION"),
    "send_recap_digests": (
        "📬 Récap abonnés", "Envoie les digests personnalisés selon la fréquence choisie", "CRON_RECAP_DIGESTS"
    ),
    "check_user_scrapers": (
        "📡 Sources personnelles", "Actualise les sources RSS/HTML selon la fréquence configurée par chaque abonné", "CRON_USER_SCRAPERS"
    ),
    "check_cloud_sync": (
        "☁️ Sync cloud", "Synchronise les connexions Google Drive et autres services cloud", "CRON_CLOUD_SYNC"
    ),
    "check_watch_alerts": (
        "🔔 Alertes de veille", "Vérifie les mots-clés surveillés et envoie les notifications par email", "CRON_WATCH_ALERTS"
    ),
}

_CRON_TASK_MAP = {
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


def parse_cron_expression(expression):
    parts = expression.split()
    if len(parts) != 5:
        return expression
    minute, hour, _, _, weekday = parts
    specials = {
        "0 * * * *": "Toutes les heures",
        "0 0 * * *": "Tous les jours à minuit",
        "0 8 * * 1": "Tous les lundis à 8h00",
        "0 6 * * *": "Tous les jours à 6h00",
    }
    if expression in specials:
        return specials[expression]
    result = []
    if minute == "0" and hour != "*":
        result.append(f"à {hour}h00")
    elif minute != "*" and hour != "*":
        result.append(f"à {hour}h{minute}")
    elif hour == "*":
        result.append("toutes les heures")
    if weekday != "*":
        days = ["Dim", "Lun", "Mar", "Mer", "Jeu", "Ven", "Sam"]
        try:
            result.append(f"le {days[int(weekday)]}")
        except (ValueError, IndexError):
            result.append(f"jour {weekday}")
    return " ".join(result) if result else expression


def get_cron_configs():
    config_file = Path(current_app.root_path) / "config" / "cron_config.json"
    custom = {}
    if config_file.exists():
        try:
            custom = json.loads(config_file.read_text())
        except Exception as e:
            logger.error(f"Erreur lecture config cron: {e}")

    configs = {}
    for task_id, (name, description, env_key) in _CRON_META.items():
        expression = custom.get(task_id) or os.getenv(env_key, _CRON_DEFAULTS[task_id])
        configs[task_id] = {
            "name": name,
            "description": description,
            "expression": expression,
            "readable": parse_cron_expression(expression),
        }
    return configs


def get_last_cron_runs():
    last_runs = {}
    cron_log_dir = Path(LOGS_DIR) / "cron"
    for task_id in _CRON_TASK_MAP:
        log_file = cron_log_dir / f"{task_id}.log"
        if not log_file.exists():
            last_runs[task_id] = "Jamais"
            continue
        try:
            lines = log_file.read_text().splitlines()
            for line in reversed(lines[-50:]):
                if "INFO" not in line and "SUCCESS" not in line:
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                try:
                    log_date = datetime.strptime(f"{parts[0]} {parts[1][:8]}", "%Y-%m-%d %H:%M:%S")
                    diff = datetime.now() - log_date
                    if diff.days == 0:
                        last_runs[task_id] = (
                            f"Il y a {diff.seconds // 60} min"
                            if diff.seconds < 3600
                            else f"Il y a {diff.seconds // 3600}h"
                        )
                    elif diff.days == 1:
                        last_runs[task_id] = "Hier"
                    else:
                        last_runs[task_id] = log_date.strftime("%d %b %Hh%M")
                    break
                except ValueError:
                    continue
            else:
                last_runs[task_id] = "Jamais"
        except Exception as e:
            logger.error(f"Erreur lecture log {task_id}: {e}")
            last_runs[task_id] = "Jamais"
    return last_runs


@admin_bp.route("/cron/<task>")
@login_required
@admin_required
def get_cron_config(task):
    configs = get_cron_configs()
    if task in configs:
        return jsonify({"expression": configs[task]["expression"], "description": configs[task]["readable"]})
    return jsonify({"expression": "", "description": ""})


@admin_bp.route("/cron/update", methods=["POST"])
@login_required
@admin_required
def update_cron_config():
    task = request.form.get("task")
    expression = request.form.get("expression")
    if not task or not expression:
        return jsonify({"success": False, "message": "Paramètres manquants"})
    tokens = expression.split()
    if len(tokens) != 5:
        return jsonify({
            "success": False,
            "message": "Expression cron invalide (format: min heure jour mois jour_semaine)",
        })
    # Bug L2 (audit round 2) — validation par-champ pour éviter qu'un
    # admin distrait casse le scheduler tenant avec une expression
    # syntaxiquement absurde (qui passait juste le check "5 tokens").
    # Pattern minimal mais couvre `*`, `*/N`, `N`, `N-M`, listes
    # `N,M`, et ranges step `N-M/K`.
    import re as _re
    _FIELD_RE = _re.compile(r"^(\*(/\d+)?|\d+(-\d+)?(/\d+)?(,\d+(-\d+)?(/\d+)?)*)$")
    for i, tok in enumerate(tokens):
        if not _FIELD_RE.match(tok):
            return jsonify({
                "success": False,
                "message": f"Champ cron #{i + 1} invalide : {tok!r}",
            })
    try:
        config_file = Path(current_app.root_path) / "config" / "cron_config.json"
        config_file.parent.mkdir(exist_ok=True)
        cron_config = json.loads(config_file.read_text()) if config_file.exists() else {}
        cron_config[task] = expression
        config_file.write_text(json.dumps(cron_config, indent=2))
        logger.info(f"Mise à jour cron {task}: {expression}")
        return jsonify({
            "success": True,
            "message": "Configuration mise à jour. Redémarrez les tâches cron pour appliquer les changements.",
        })
    except Exception as e:
        logger.error(f"Erreur mise à jour cron {task}: {e}")
        return jsonify({"success": False, "message": str(e)})


@admin_bp.route("/cron/run/<task>", methods=["POST"])
@login_required
@admin_required
def run_cron_task(task):
    if task not in _CRON_TASK_MAP:
        return jsonify({"success": False, "message": "Tâche inconnue"})
    configs = get_cron_configs()
    config = configs.get(task)
    if not config:
        return jsonify({"success": False, "message": "Tâche non configurée"})
    module_path, func_name = _CRON_TASK_MAP[task]
    try:
        module = importlib.import_module(module_path)
        raw = getattr(module, func_name)
        func = getattr(raw, "callback", raw)
        app = current_app._get_current_object()

        def _run():
            with app.app_context():
                try:
                    func()
                except Exception as exc:
                    logger.error(f"Erreur tâche cron {task}: {exc}")

        threading.Thread(target=_run, daemon=True).start()
        logger.info(f"Tâche {task} lancée manuellement en arrière-plan")
        return jsonify({"success": True, "message": f"Tâche {config['name']} lancée"})
    except Exception as e:
        logger.error(f"Erreur lancement tâche {task}: {e}")
        return jsonify({"success": False, "message": str(e)})
