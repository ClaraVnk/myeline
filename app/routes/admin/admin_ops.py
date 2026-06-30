import os
import re
import threading
from datetime import datetime, timedelta
from typing import Optional

import sqlalchemy as sa
from flask import current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.ext import db
from app.routes.admin.admin import LOGS_DIR, REPORTS_DIR, admin_bp, admin_required
from app.routes.admin.admin_cron import get_cron_configs, get_last_cron_runs
from app.utils.flask_limiter import conditional_limit
from app.utils.logger import get_logger
from app.utils.time_utils import utcnow


logger = get_logger("admin_ops", subfolder="routes")


@admin_bp.route("/tools")
@login_required
@admin_required
def tools_dashboard():
    try:
        log_files = [f for f in os.listdir(LOGS_DIR) if os.path.isfile(os.path.join(LOGS_DIR, f))]
        log_sizes = {}
        for log in log_files:
            size = os.path.getsize(os.path.join(LOGS_DIR, log))
            log_sizes[log] = (
                f"{size / 1024:.1f} KB" if size < 1024 * 1024 else f"{size / (1024 * 1024):.1f} MB"
            )
    except Exception as e:
        logger.error(f"Erreur lecture logs: {e}")
        log_files = []
        log_sizes = {}

    try:
        report_files = [
            f for f in os.listdir(REPORTS_DIR) if os.path.isfile(os.path.join(REPORTS_DIR, f))
        ]
        report_sizes = {}
        for report in report_files:
            size = os.path.getsize(os.path.join(REPORTS_DIR, report))
            report_sizes[report] = (
                f"{size / 1024:.1f} KB" if size < 1024 * 1024 else f"{size / (1024 * 1024):.1f} MB"
            )
    except Exception as e:
        logger.error(f"Erreur lecture reports: {e}")
        report_files = []
        report_sizes = {}

    cron_configs = get_cron_configs()
    cron_last_runs = get_last_cron_runs()

    return render_template(
        "admin/tools_dashboard.html",
        log_files=log_files,
        log_sizes=log_sizes,
        report_files=report_files,
        report_sizes=report_sizes,
        cron_configs=cron_configs,
        cron_last_runs=cron_last_runs,
    )


@admin_bp.route("/tools/clear-logs", methods=["POST"])
@login_required
@admin_required
def clear_logs():
    try:
        count = 0
        for log_file in os.listdir(LOGS_DIR):
            file_path = os.path.join(LOGS_DIR, log_file)
            if os.path.isfile(file_path):
                if os.path.getmtime(file_path) < (datetime.now().timestamp() - 30 * 24 * 3600):
                    os.remove(file_path)
                    count += 1
        return jsonify({"success": True, "message": f"{count} fichiers supprimés"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


# Audit round 4 — A4-C1. Previously a single POST flushed the entire Redis
# database without confirmation, rate limit, or audit trail — one mis-click
# wiped session cookies, broadcast idempotency markers, embed-reindex locks
# and per-tenant quotas at once. Two-step flow now:
#   1. GET /tools/clear-cache/preview returns the current key count and a
#      short-lived signed token (TTL 30s) tied to the admin id + key count.
#   2. POST /tools/clear-cache validates the token + matches the key count
#      against what the admin saw at preview time. A drift means keys were
#      added between preview and confirm — we refuse rather than wipe a
#      moving target.
# flushdb() only affects the database the URL points to (typically /0); we
# never call flushall(). Rate limit: 1 / hour / admin.
_CLEAR_CACHE_TOKEN_TTL = 30
_CLEAR_CACHE_TOKEN_SALT = "admin-clear-cache"


def _redis_client_for_clear():
    import redis as _redis
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    return _redis.from_url(redis_url, socket_connect_timeout=2)


@admin_bp.route("/tools/clear-cache/preview", methods=["GET"])
@login_required
@admin_required
def clear_cache_preview():
    """Step 1 — report the current Redis key count and issue a confirm token.

    The token is bound to (admin_id, dbsize) so an operator who calls preview,
    sees N keys and confirms can only wipe a database that still has N keys.
    Anything else (cache warm-up between steps, concurrent admin) refuses
    the wipe.
    """
    from itsdangerous import URLSafeTimedSerializer
    try:
        r = _redis_client_for_clear()
        dbsize = int(r.dbsize())
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 503
    serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"])
    token = serializer.dumps(
        {"admin_id": current_user.id, "dbsize": dbsize},
        salt=_CLEAR_CACHE_TOKEN_SALT,
    )
    return jsonify({
        "success": True,
        "dbsize": dbsize,
        "confirm_token": token,
        "expires_in": _CLEAR_CACHE_TOKEN_TTL,
    })


@admin_bp.route("/tools/clear-cache", methods=["POST"])
@login_required
@admin_required
@conditional_limit("1 per hour", key_func=lambda: f"clear-cache:{current_user.id}")
def clear_cache():
    """Step 2 — wipe the current Redis DB after validating the confirm token.

    The token must:
    - be ≤ _CLEAR_CACHE_TOKEN_TTL old (rejected otherwise),
    - have been issued to the same admin user, and
    - carry the same dbsize the admin observed at preview time.
    """
    from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

    from app.utils.audit import log_audit

    payload = request.get_json(silent=True) or request.form
    token = (payload.get("confirm_token") or "").strip()
    if not token:
        return jsonify({
            "success": False,
            "message": "Token de confirmation manquant — appelez d'abord /tools/clear-cache/preview.",
        }), 400

    serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"])
    try:
        data = serializer.loads(
            token, salt=_CLEAR_CACHE_TOKEN_SALT, max_age=_CLEAR_CACHE_TOKEN_TTL,
        )
    except SignatureExpired:
        return jsonify({
            "success": False,
            "message": "Token expiré — refaites un preview.",
        }), 400
    except BadSignature:
        logger.warning(
            "clear_cache refused: invalid token (admin=%s)", current_user.id,
        )
        return jsonify({"success": False, "message": "Token invalide"}), 400

    if data.get("admin_id") != current_user.id:
        logger.warning(
            "clear_cache refused: token admin_id=%s != current=%s",
            data.get("admin_id"), current_user.id,
        )
        return jsonify({"success": False, "message": "Token non rattaché à cet admin"}), 403

    try:
        r = _redis_client_for_clear()
        current_dbsize = int(r.dbsize())
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 503

    if data.get("dbsize") != current_dbsize:
        return jsonify({
            "success": False,
            "message": (
                f"La taille de la base a changé depuis le preview "
                f"({data.get('dbsize')} → {current_dbsize}). Refaites un preview."
            ),
        }), 409

    try:
        r.flushdb()
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

    log_audit(
        "admin.clear_cache",
        target_type="redis_db", target_id=None,
        payload={"keys_flushed": current_dbsize},
    )
    logger.warning(
        "Redis DB flushed by admin=%s (keys_flushed=%d)",
        current_user.id, current_dbsize,
    )
    return jsonify({
        "success": True,
        "message": f"Cache vidé : {current_dbsize} clés effacées.",
        "keys_flushed": current_dbsize,
    })


@admin_bp.route("/tools/restart", methods=["POST"])
@login_required
@admin_required
def restart_app():
    # Bug L5 (audit round 2) — hardcodé `docker compose` alors que le
    # projet tourne sous Podman (cf. CLAUDE.md règle 12). En prod on
    # n'a pas le binaire docker → FileNotFoundError silencieux côté
    # client. CONTAINER_ENGINE permet l'override (legacy docker en dev,
    # podman en prod).
    import os as _os
    import subprocess  # nosec B404 - Admin-only, fixed command
    engine = _os.getenv("CONTAINER_ENGINE", "podman")
    try:
        # podman-compose et docker compose ont la même UX restart.
        cmd = (["podman-compose", "restart", "web"] if engine == "podman"
               else [engine, "compose", "restart", "web"])
        subprocess.Popen(cmd)  # nosec B603 - admin-only, fixed-set cmd
        return jsonify({"success": True, "message": "Redémarrage en cours..."})
    except FileNotFoundError:
        return jsonify({
            "success": False,
            "message": f"Binaire {engine!r} introuvable côté container web.",
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@admin_bp.route("/tools/export-data")
@login_required
@admin_required
def export_data():
    import json

    from app.models.newsletter import Newsletter
    from app.models.user import User

    try:
        data = {
            "users": [
                {"email": u.email, "firstname": u.firstname, "lastname": u.lastname}
                for u in User.query.all()
            ],
            "newsletters": [
                {"id": n.id, "date": str(n.date), "subject": n.title}
                for n in Newsletter.query.all()
            ],
            "export_date": datetime.now().isoformat(),
        }
        response = current_app.response_class(
            response=json.dumps(data, indent=2), status=200, mimetype="application/json"
        )
        response.headers["Content-Disposition"] = (
            f'attachment; filename=export_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
        )
        return response
    except Exception as e:
        flash(f"Erreur lors de l'export: {str(e)}", "danger")
        return redirect(url_for("admin.tools_dashboard"))


# Audit round 4 — A4-C2. The previous version joined `filename` directly to
# LOGS_DIR / REPORTS_DIR, so a payload like "..%2F..%2Fetc%2Fpasswd" escaped
# the parent directory and let an admin delete any file the process could
# touch. We now require basename-only filenames, validate against a strict
# regex, and double-check the resolved real path is still inside the
# allowed directory (defence in depth against symlinks).
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _resolve_safe_admin_path(base_dir: str, filename: str) -> Optional[str]:
    """Return the absolute path for `filename` inside `base_dir`, or None.

    Returns None when the filename is empty, contains path separators, fails
    the safe-filename regex, or escapes ``base_dir`` after symlink resolution.
    Caller treats None as "404, do not attempt the operation".
    """
    if not filename or os.sep in filename or "/" in filename or "\\" in filename:
        return None
    safe = os.path.basename(filename)
    if safe != filename or not _SAFE_FILENAME_RE.match(safe):
        return None
    base_real = os.path.realpath(base_dir)
    candidate = os.path.realpath(os.path.join(base_real, safe))
    if not candidate.startswith(base_real + os.sep) and candidate != base_real:
        return None
    return candidate


@admin_bp.route("/tools/delete-log/<filename>", methods=["DELETE"])
@login_required
@admin_required
def delete_log(filename):
    from app.utils.audit import log_audit
    try:
        file_path = _resolve_safe_admin_path(LOGS_DIR, filename)
        if file_path is None:
            logger.warning(
                "delete_log refused unsafe filename=%r (admin=%s)",
                filename, current_user.id,
            )
            return jsonify({"success": False, "message": "Nom de fichier invalide"}), 400
        if os.path.isfile(file_path):
            os.remove(file_path)
            log_audit(
                "admin.delete_log",
                target_type="log_file", target_id=None,
                payload={"filename": os.path.basename(file_path)},
            )
            return jsonify({"success": True, "message": "Log supprimé"})
        return jsonify({"success": False, "message": "Fichier introuvable"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@admin_bp.route("/tools/delete-report/<filename>", methods=["DELETE"])
@login_required
@admin_required
def delete_report(filename):
    from app.utils.audit import log_audit
    try:
        file_path = _resolve_safe_admin_path(REPORTS_DIR, filename)
        if file_path is None:
            logger.warning(
                "delete_report refused unsafe filename=%r (admin=%s)",
                filename, current_user.id,
            )
            return jsonify({"success": False, "message": "Nom de fichier invalide"}), 400
        if os.path.isfile(file_path):
            os.remove(file_path)
            log_audit(
                "admin.delete_report",
                target_type="report_file", target_id=None,
                payload={"filename": os.path.basename(file_path)},
            )
            return jsonify({"success": True, "message": "Rapport supprimé"})
        return jsonify({"success": False, "message": "Fichier introuvable"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@admin_bp.route("/api/stats")
@login_required
@admin_required
def api_stats():
    from app.models.user import User
    from app.services.rag_service import get_index_stats

    # ── Subscribers ──────────────────────────────────────────────────────────
    # OSS single-user build: no billing tiers. Count active, non-admin,
    # non-excluded accounts.
    total_pro = User.query.filter(
        User.is_admin.is_(False),
        User.exclude_from_stats.is_(False),
    ).count()

    # Growth: new accounts per month — always 12 months, filling 0 for empty months
    twelve_months_ago = utcnow() - timedelta(days=365)
    rows = db.session.execute(
        sa.text(
            "SELECT DATE_FORMAT(created_at, '%Y-%m') AS month, COUNT(*) AS n "
            "FROM users "
            "WHERE created_at >= :since AND is_admin = 0 "
            "AND exclude_from_stats = 0 "
            "GROUP BY month ORDER BY month"
        ),
        {"since": twelve_months_ago},
    ).fetchall()
    db_growth = {r[0]: r[1] for r in rows}
    # Build the full 12-month series with zeroes for empty months
    now = utcnow()
    growth = []
    for i in range(11, -1, -1):
        m = now - timedelta(days=i * 30)
        key = m.strftime("%Y-%m")
        growth.append({"month": key, "count": db_growth.get(key, 0)})

    # ── RAG ──────────────────────────────────────────────────────────────────
    rag = get_index_stats()

    # ── User scrapers & personal libraries ──────────────────────────────────
    from app.models.user_document import UserDocument
    from app.models.user_scraper import UserScraper
    user_scraper_count = UserScraper.query.count()
    personal_lib_bytes = db.session.query(
        sa.func.coalesce(sa.func.sum(UserDocument.file_size), 0)
    ).scalar() or 0

    return jsonify({
        "subscribers": {
            "pro": total_pro,
            "total": total_pro,
            "growth": growth,
        },
        "rag": {
            "indexed_sections": rag.get("indexed_sections", 0),
            "embed_model": rag.get("embed_model", ""),
            "llm_model": rag.get("llm_model", ""),
            "status": rag.get("status", ""),
        },
        "user_content": {
            "scraper_count": user_scraper_count,
            "personal_lib_bytes": personal_lib_bytes,
        },
    })


@admin_bp.route("/api/sysmetrics")
@login_required
@admin_required
def api_sysmetrics():
    """Return live CPU / RAM / disk stats and Ollama reachability.

    Hardened to never 500: any sub-probe failure (psutil quirk in a
    minimal container, missing /, Ollama down) is swallowed and a
    null is returned for that field. The frontend can render the
    rest of the panel even when one probe is unhappy.
    """
    import os as _os

    import psutil
    import requests as _req

    payload: dict = {}

    try:
        payload["cpu_pct"] = round(psutil.cpu_percent(interval=0.3), 1)
    except Exception as exc:
        logger.warning("psutil cpu_percent failed: %s", exc)
        payload["cpu_pct"] = None

    try:
        vm = psutil.virtual_memory()
        payload["ram_pct"] = round(vm.percent, 1)
        payload["ram_used_gb"] = round(vm.used / 1024**3, 2)
        payload["ram_total_gb"] = round(vm.total / 1024**3, 2)
    except Exception as exc:
        logger.warning("psutil virtual_memory failed: %s", exc)
        payload["ram_pct"] = payload["ram_used_gb"] = payload["ram_total_gb"] = None

    try:
        disk = psutil.disk_usage("/")
        payload["disk_pct"] = round(disk.percent, 1)
        payload["disk_used_gb"] = round(disk.used / 1024**3, 1)
        payload["disk_total_gb"] = round(disk.total / 1024**3, 1)
    except Exception as exc:
        logger.warning("psutil disk_usage('/') failed: %s", exc)
        payload["disk_pct"] = payload["disk_used_gb"] = payload["disk_total_gb"] = None

    # Ollama ping. Canonical env var is OLLAMA_URL (rag_service / cron /
    # /health all read it). The previous lookup of `OLLAMA_BASE_URL`
    # via config never matched a configured value in production.
    ollama_url = _os.getenv("OLLAMA_URL", "http://ollama:11434")
    try:
        r = _req.get(f"{ollama_url}/api/tags", timeout=1.5)
        payload["ollama_ok"] = r.status_code == 200
        payload["ollama_models"] = [m.get("name", "") for m in r.json().get("models", [])]
    except Exception:
        payload["ollama_ok"] = False
        payload["ollama_models"] = []

    return jsonify(payload)


@admin_bp.route("/api/set-llm-provider", methods=["POST"])
@login_required
@admin_required
def set_llm_provider():
    """Store the LLM provider preference in Redis so rag_service picks it up immediately."""
    from flask import request as _req

    from app.services.rag_service import _LLM_PROVIDER_KEY, _MISTRAL_API_KEY

    data = _req.get_json(silent=True) or {}
    provider = data.get("provider", "").lower()
    if provider not in ("mistral", "ollama"):
        return jsonify({"success": False, "error": "Invalid provider"}), 400

    if provider == "mistral" and not _MISTRAL_API_KEY:
        return jsonify({"success": False, "error": "No Mistral API key configured"}), 400

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        return jsonify({"success": False, "error": "Redis not configured"}), 500

    try:
        from redis import Redis
        r = Redis.from_url(redis_url, socket_connect_timeout=2)
        r.set(_LLM_PROVIDER_KEY, provider)
        logger.info("LLM provider switched to %s by admin", provider)
        return jsonify({"success": True, "provider": provider})
    except Exception as e:
        logger.error("Failed to set LLM provider in Redis: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@admin_bp.route("/api/get-llm-provider")
@login_required
@admin_required
def get_llm_provider():
    """Return the current LLM provider setting."""
    from app.services.rag_service import _LLM_PROVIDER_KEY, _MISTRAL_API_KEY

    redis_url = os.getenv("REDIS_URL")
    current = "mistral" if _MISTRAL_API_KEY else "ollama"  # default
    if redis_url:
        try:
            from redis import Redis
            val = Redis.from_url(redis_url, socket_connect_timeout=1).get(_LLM_PROVIDER_KEY)
            if val in (b"mistral", b"ollama"):
                current = val.decode()
        except Exception:
            pass
    return jsonify({
        "provider": current,
        "mistral_configured": bool(_MISTRAL_API_KEY),
        "sovereign_mode": os.getenv("SOVEREIGN_MODE", "false").lower() in ("true", "1", "yes"),
    })


@admin_bp.route("/api/local-llm-models")
@login_required
@admin_required
def list_local_llm_models():
    """List all Ollama models pulled on the host, plus the currently active one."""
    import requests as _req

    from app.services.rag_service import _LLM_MODEL, _LOCAL_MODEL_KEY, _resolved_local_model

    ollama_url = os.getenv("OLLAMA_URL", "http://ollama:11434")
    models = []
    error = None
    try:
        r = _req.get(f"{ollama_url}/api/tags", timeout=3)
        r.raise_for_status()
        # Ollama returns chat + embedding models mixed; filter out embedders so
        # the picker only shows valid synthesis candidates.
        for m in r.json().get("models", []):
            name = m.get("name", "")
            if any(tag in name.lower() for tag in ("embed", "bge-", "nomic")):
                continue
            details = m.get("details") or {}
            models.append({
                "name": name,
                "size": m.get("size", 0),
                "param_size": details.get("parameter_size", ""),
                "quantization": details.get("quantization_level", ""),
                "family": details.get("family", ""),
            })
    except Exception as e:
        error = str(e)
    models.sort(key=lambda x: x["name"])
    return jsonify({
        "models": models,
        "active": _resolved_local_model(),
        "default_env": _LLM_MODEL,
        "error": error,
    })


# Audit round 4 — A4-H1. The two set-local-*-model endpoints used to
# accept the user-supplied model string verbatim before writing it to
# Redis, and the only filter was a JS-side allowlist driven by the
# /api/local-*-models response. A future JS bug, a tampered admin
# session (XSS) or a direct API call (curl) could thus set _LOCAL_*_KEY
# to "../malicious", an OS command marker, or a model name pointing at
# an external Ollama endpoint we don't trust. We now re-query Ollama
# server-side from the same /api/tags endpoint the listing uses, and
# refuse anything that isn't in that pulled-model set.
def _fetch_pulled_ollama_models() -> tuple:
    """Return (set_of_pulled_model_names, error_message_or_None).

    Trims the names with .strip() so trailing newlines / accidental
    whitespace don't sneak past the membership check.
    """
    import requests as _req
    ollama_url = os.getenv("OLLAMA_URL", "http://ollama:11434")
    try:
        r = _req.get(f"{ollama_url}/api/tags", timeout=3)
        r.raise_for_status()
        names = {
            (m.get("name") or "").strip()
            for m in r.json().get("models", [])
            if (m.get("name") or "").strip()
        }
        return names, None
    except Exception as exc:
        return set(), str(exc)


@admin_bp.route("/api/set-local-llm-model", methods=["POST"])
@login_required
@admin_required
def set_local_llm_model():
    """Persist the Ollama model used for synthesis (Redis override).

    The submitted model name MUST appear in the live ``/api/tags`` listing
    from Ollama; an unknown name is rejected with 400. Ollama down →
    refuse (fail-safe) — we can't validate, so we don't write.
    """
    from flask import request as _req

    from app.services.rag_service import _LOCAL_MODEL_KEY

    data = _req.get_json(silent=True) or {}
    model = (data.get("model") or "").strip()
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        return jsonify({"success": False, "error": "Redis not configured"}), 500
    if model:
        pulled, fetch_err = _fetch_pulled_ollama_models()
        if fetch_err:
            logger.error(
                "set_local_llm_model refused: cannot reach Ollama for whitelist: %s",
                fetch_err,
            )
            return jsonify({
                "success": False,
                "error": "Impossible de joindre Ollama pour valider le modèle — réessayez.",
            }), 503
        if model not in pulled:
            logger.warning(
                "set_local_llm_model refused: model=%r not in pulled set (%d entries)",
                model, len(pulled),
            )
            return jsonify({
                "success": False,
                "error": f"Modèle {model!r} introuvable parmi les modèles Ollama installés.",
            }), 400
    try:
        from redis import Redis
        r = Redis.from_url(redis_url, socket_connect_timeout=2)
        if not model:
            r.delete(_LOCAL_MODEL_KEY)  # revert to env default
            logger.info("Local LLM model override cleared by admin")
            return jsonify({"success": True, "model": None})
        r.set(_LOCAL_MODEL_KEY, model)
        logger.info("Local LLM model switched to %s by admin", model)
        return jsonify({"success": True, "model": model})
    except Exception as e:
        logger.error("Failed to set local LLM model in Redis: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@admin_bp.route("/api/local-embed-models")
@login_required
@admin_required
def list_local_embed_models():
    """List Ollama embedding models pulled on the host + the active one + reindex lock state.

    The picker filters to genuine embedders by name pattern (bge-*, nomic*,
    *embed*, mxbai-*, arctic*, paraphrase-*, minilm*) — the same Ollama
    /api/tags endpoint also returns chat models, which we exclude.
    """
    import requests as _req

    from app.services.rag_service import _LOCAL_EMBED_MODEL_KEY  # noqa: F401  — exposed for completeness
    from app.services.rag_service import _EMBED_MODEL, _EMBED_REINDEX_LOCK_KEY, _active_embed_model

    ollama_url = os.getenv("OLLAMA_URL", "http://ollama:11434")
    models = []
    error = None
    try:
        r = _req.get(f"{ollama_url}/api/tags", timeout=3)
        r.raise_for_status()
        for m in r.json().get("models", []):
            name = (m.get("name") or "").lower()
            # Keep only embedders. The pattern list is conservative — better
            # to show too many than to hide a valid choice.
            if not any(tag in name for tag in (
                "embed", "bge-", "nomic", "mxbai-", "arctic",
                "paraphrase-", "minilm",
            )):
                continue
            details = m.get("details") or {}
            models.append({
                "name": m.get("name", ""),
                "size": m.get("size", 0),
                "param_size": details.get("parameter_size", ""),
                "quantization": details.get("quantization_level", ""),
                "family": details.get("family", ""),
            })
    except Exception as e:
        error = str(e)
    models.sort(key=lambda x: x["name"])

    # Reindex lock state — UI uses it to show a "reindex in progress" badge
    # and refuse a new submit until the running job completes.
    reindex_in_progress = False
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        try:
            from redis import Redis
            reindex_in_progress = bool(
                Redis.from_url(redis_url, socket_connect_timeout=1).get(_EMBED_REINDEX_LOCK_KEY)
            )
        except Exception:
            pass

    return jsonify({
        "models": models,
        "active": _active_embed_model(),
        "default_env": _EMBED_MODEL,
        "reindex_in_progress": reindex_in_progress,
        "error": error,
    })


@admin_bp.route("/api/set-local-embed-model", methods=["POST"])
@login_required
@admin_required
def set_local_embed_model():
    """Persist the Ollama embedder override AND enqueue a corpus reindex.

    Switching the embed model requires re-embedding every chunk — the
    new model's vectors are not comparable to the old ones. We:
      1. Hold the Redis reindex lock (refuses concurrent switches).
      2. Persist the override key so subsequent queries use the new model.
      3. Enqueue an RQ job that walks every collection and re-embeds.
    The lock is released by the RQ task itself (success or failure).
    """
    from flask import request as _req

    from app.services.rag_service import _EMBED_REINDEX_LOCK_KEY, _LOCAL_EMBED_MODEL_KEY

    data = _req.get_json(silent=True) or {}
    model = (data.get("model") or "").strip()
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        return jsonify({"success": False, "error": "Redis not configured"}), 500

    # Audit round 4 — A4-H1. Same defence-in-depth check as
    # set_local_llm_model: reject anything that isn't in Ollama's live
    # /api/tags listing.
    if model:
        pulled, fetch_err = _fetch_pulled_ollama_models()
        if fetch_err:
            logger.error(
                "set_local_embed_model refused: cannot reach Ollama for whitelist: %s",
                fetch_err,
            )
            return jsonify({
                "success": False,
                "error": "Impossible de joindre Ollama pour valider le modèle — réessayez.",
            }), 503
        if model not in pulled:
            logger.warning(
                "set_local_embed_model refused: model=%r not in pulled set (%d entries)",
                model, len(pulled),
            )
            return jsonify({
                "success": False,
                "error": f"Modèle {model!r} introuvable parmi les modèles Ollama installés.",
            }), 400

    try:
        from redis import Redis
        r = Redis.from_url(redis_url, socket_connect_timeout=2)

        # Refuse if a reindex is already in flight — avoids two concurrent
        # reindex jobs trampling the same ChromaDB collections.
        if r.get(_EMBED_REINDEX_LOCK_KEY):
            return jsonify({
                "success": False,
                "error": "Une réindexation est déjà en cours. Réessayez quand elle sera terminée.",
            }), 409

        if not model:
            # Empty = revert to env default. No reindex needed only if the
            # active model was already the env default (cheap path); we don't
            # have visibility on that without re-resolving — safer to always
            # reindex so the corpus matches whatever we now resolve to.
            r.delete(_LOCAL_EMBED_MODEL_KEY)
            logger.info("Local embed model override cleared by admin (revert to env default)")
        else:
            r.set(_LOCAL_EMBED_MODEL_KEY, model)
            logger.info("Local embed model switched to %s by admin (reindex enqueued)", model)

        # Lock + enqueue. TTL 6h: a reindex on a Pro corpus runs minutes,
        # but a very large Sovereign corpus could take longer; 6h is the
        # crash-safety ceiling, the task's own `finally` clears it first.
        r.set(_EMBED_REINDEX_LOCK_KEY, "1", nx=False, ex=6 * 3600)

        from rq import Queue
        q = Queue("myeline", connection=r)
        job = q.enqueue(
            "app.tasks.reindex_embeddings_task",
            job_timeout=6 * 3600,
            result_ttl=86400 * 7,
            failure_ttl=86400 * 30,
        )
        return jsonify({
            "success": True,
            "model": model or None,
            "reindex_job_id": job.id,
        })
    except Exception as e:
        logger.error("Failed to set local embed model: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


_PLAN_LABELS = {
    "premium_pro": "Pro",
    "enterprise": "Enterprise",
}

# Audit round 4 — A4-M3. Subscriber count above which a broadcast POST
# must echo back the exact recipient count as a hidden ``confirm_count``
# form field. Hoisted to module scope so tests can monkeypatch it
# without depending on a 100-row seed (which polluted the SQLite
# database and broke neighbouring tests).
_BROADCAST_LARGE_THRESHOLD = 100


def _subscriber_base():
    from app.models.user import User
    return User.query.filter(
        User.is_active.is_(True),
        User.is_admin.is_(False),
    )


@admin_bp.route("/broadcast", methods=["GET", "POST"])
@login_required
@admin_required
def broadcast_email():
    from flask import request

    from app.queue import get_queue
    from app.tasks import broadcast_task

    # Counts per plan for the UI
    counts = {plan: _subscriber_base().filter_by(abonnement_type=plan).count()
              for plan in _PLAN_LABELS}
    counts["total"] = sum(counts.values())

    if request.method == "POST":
        subject = request.form.get("subject", "").strip()
        html_content = request.form.get("html_content", "").strip()
        if not subject or not html_content:
            flash("Sujet et contenu requis.", "warning")
            return redirect(url_for("admin.broadcast_email"))

        # Bug X8 (audit round 3) — Quill côté admin ne sanitize pas sa
        # sortie. Un admin compromis (ou XSS futur dans l'admin UI)
        # pouvait injecter <script>/<iframe>/onerror dans l'email
        # diffusé à tous les abonnés. Whitelist stricte de tags + attrs
        # alignée sur la toolbar Quill de broadcast.html.
        import bleach as _bleach
        _ALLOWED_TAGS = {
            "a", "p", "strong", "em", "u", "s", "br", "hr",
            "ul", "ol", "li",
            "h1", "h2", "h3", "h4",
            "blockquote", "pre", "code",
            "span", "div",
        }
        _ALLOWED_ATTRS = {
            "a": ["href", "target", "rel"],
            "span": ["class"],
            "div": ["class"],
        }
        html_content = _bleach.clean(
            html_content,
            tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS,
            protocols=["http", "https", "mailto"],
            strip=True,
        )
        if not html_content.strip():
            flash("Contenu vide après nettoyage HTML.", "warning")
            return redirect(url_for("admin.broadcast_email"))

        plans_filter = request.form.getlist("plans")
        base_q = _subscriber_base()
        if plans_filter and "all" not in plans_filter:
            from app.models.user import User
            base_q = base_q.filter(User.abonnement_type.in_(plans_filter))
        subscribers = base_q.all()

        if not subscribers:
            flash("Aucun destinataire pour cette sélection.", "warning")
            return redirect(url_for("admin.broadcast_email"))

        recipient_ids = [u.id for u in subscribers]

        # Audit round 4 — A4-M3. Once the subscriber count crosses
        # _BROADCAST_LARGE_THRESHOLD (defined at module scope), require
        # the admin to echo back the *exact* count as a hidden
        # confirm_count form field. A click-by-mistake or a CSRF that
        # survived SameSite=Lax but doesn't know the live count can't
        # push the form through. The JS confirm() dialog in the
        # template is best-effort; this is the server-side enforcement.
        if len(subscribers) > _BROADCAST_LARGE_THRESHOLD:
            try:
                claimed_count = int((request.form.get("confirm_count") or "").strip())
            except (TypeError, ValueError):
                claimed_count = None
            if claimed_count != len(subscribers):
                logger.warning(
                    "broadcast_large_confirmation_missing actual=%d claimed=%r admin=%s",
                    len(subscribers), claimed_count, current_user.id,
                )
                flash(
                    f"Ce broadcast cible {len(subscribers)} destinataires. "
                    f"Pour confirmer, ajoutez le champ caché confirm_count={len(subscribers)} "
                    f"au formulaire et relancez l'envoi.",
                    "warning",
                )
                return redirect(url_for("admin.broadcast_email"))

        # Idempotency lock: hash (subject + body + sorted recipients) and try
        # to claim a 1-hour key in Redis with NX. A double-submit (network
        # blip, frantic re-click, browser back-then-resubmit) lands here a
        # second time, the SETNX fails, and we refuse to re-enqueue. The
        # admin can still send the same payload again after 1h, or change
        # any character to bypass.
        #
        # Audit round 4 — A4-C3. Previously the Redis-down fallback was
        # fail-OPEN (claimed=True), so a double-submit during a Redis
        # outage would send the broadcast twice — duplicated mass-email,
        # impossible to recall. Policy is now fail-SAFE: Redis unreachable
        # → refuse the broadcast with HTTP 503 + a clear flash. The admin
        # can retry once Redis is back. A blocked broadcast is strictly
        # preferable to a duplicated one.
        import hashlib as _hashlib
        payload_hash = _hashlib.sha256(
            (subject + "\x1e" + html_content + "\x1e" + ",".join(map(str, sorted(recipient_ids)))).encode()
        ).hexdigest()[:32]
        lock_key = f"broadcast:lock:{payload_hash}"
        try:
            from redis import Redis
            redis_conn = Redis.from_url(current_app.config["REDIS_URL"])
            claimed = redis_conn.set(lock_key, "1", nx=True, ex=3600)
        except Exception as exc:
            logger.error(
                "broadcast_redis_unavailable err=%s — refusing to send without dedup",
                exc,
            )
            flash(
                "Le verrou anti-doublon (Redis) est indisponible. "
                "Broadcast refusé pour ne pas risquer un envoi en double. "
                "Réessayez dans quelques minutes.",
                "danger",
            )
            return redirect(url_for("admin.broadcast_email"))
        if not claimed:
            flash(
                "Ce broadcast a déjà été envoyé dans la dernière heure "
                "(même sujet, contenu et destinataires). "
                "Modifiez le contenu ou attendez 1h pour réessayer.",
                "warning",
            )
            return redirect(url_for("admin.broadcast_email"))

        q = get_queue(current_app._get_current_object(), "myeline")
        job = q.enqueue(
            broadcast_task,
            recipient_ids,
            subject,
            html_content,
            request.url_root,
            job_timeout=600,
        )
        from app.utils.audit import log_audit
        log_audit(
            "broadcast.send",
            target_type="broadcast", target_id=job.id,
            payload={
                "subject": subject[:120],
                "recipient_count": len(recipient_ids),
                "plans": list(plans_filter),
                "idempotency_key": payload_hash,
            },
        )
        from app.ext import db as _db
        _db.session.commit()
        flash(f"Broadcast enqueued : {len(recipient_ids)} destinataire(s).", "success")
        return redirect(url_for("admin.broadcast_email"))

    return render_template("admin/broadcast.html", counts=counts, plan_labels=_PLAN_LABELS)


@admin_bp.route("/rag/stats")
@login_required
@admin_required
def rag_stats():
    from app.services.rag_service import get_index_stats

    return jsonify(get_index_stats())


@admin_bp.route("/rag/index-all", methods=["POST"])
@login_required
@admin_required
def rag_index_all():
    from app.services.rag_service import index_all_newsletters

    app = current_app._get_current_object()

    def _run():
        with app.app_context():
            try:
                index_all_newsletters()
            except Exception:
                logger.exception("Erreur indexation RAG en arrière-plan")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"success": True, "message": "Indexation lancée en arrière-plan."})


@admin_bp.route("/rag/reindex", methods=["POST"])
@login_required
@admin_required
def rag_reindex():
    """Vide la collection ChromaDB puis ré-indexe toutes les newsletters (migration de modèle d'embedding)."""
    from app.services.rag_service import index_all_newsletters

    app = current_app._get_current_object()

    def _run():
        with app.app_context():
            try:
                index_all_newsletters(clear_first=True)
            except Exception:
                logger.exception("Erreur ré-indexation RAG en arrière-plan")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"success": True, "message": "Ré-indexation complète lancée en arrière-plan."})
