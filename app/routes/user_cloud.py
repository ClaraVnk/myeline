import threading

import redis as redis_lib
from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required

from app.ext import db
from app.models.cloud_connection import PROVIDER_LABELS, SUPPORTED_PROVIDERS, CloudConnection, CloudDocument
from app.utils.logger import get_logger


def _redis() -> redis_lib.Redis:
    url = current_app.config.get("REDIS_URL", "redis://localhost:6379/0")
    return redis_lib.from_url(url, decode_responses=True)


def _store_oauth_state(provider: str, state: str, user_id: int) -> None:
    """Persist OAuth state in Redis (10-min TTL) + session as backup."""
    try:
        _redis().setex(f"oauth_state:{state}", 600, f"{user_id}:{provider}")
    except Exception as e:
        logger.warning("Redis unavailable for OAuth state storage: %s", e)
    session[f"{provider}_oauth_state"] = state


def _pop_oauth_state(provider: str, state: str, user_id: int) -> bool:
    """Validate and consume OAuth state. Returns True if valid."""
    # Check Redis first (more reliable across proxy/redirect hops)
    try:
        r = _redis()
        key = f"oauth_state:{state}"
        stored = r.get(key)
        if stored and stored == f"{user_id}:{provider}":
            r.delete(key)
            session.pop(f"{provider}_oauth_state", None)
            return True
    except Exception as e:
        logger.warning("Redis unavailable for OAuth state check, falling back to session: %s", e)

    # Fallback: session-based check
    expected = session.pop(f"{provider}_oauth_state", None)
    return expected is not None and expected == state


logger = get_logger("user_cloud", subfolder="routes")

user_cloud_bp = Blueprint("user_cloud", __name__)


def _redirect_uri(endpoint: str, **kwargs) -> str:
    """Build an absolute redirect URI, honouring OAUTH_REDIRECT_BASE_URL if set."""
    base = current_app.config.get("OAUTH_REDIRECT_BASE_URL", "").rstrip("/")
    path = url_for(endpoint, **kwargs)
    if base:
        # Bug M13 — l'ancien code concaténait base + path sans valider :
        # un misconfig pouvait pointer le callback OAuth vers un domaine
        # tiers (open redirect du code d'autorisation, donc fuite du
        # code → exfiltration du token). Fix : valider scheme + netloc ;
        # fallback safe sur url_for(_external=True) si la valeur est
        # douteuse.
        from urllib.parse import urlparse
        parsed = urlparse(base)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            logger.error(
                "OAUTH_REDIRECT_BASE_URL invalide (%r) — fallback sur url_for(_external=True)",
                base,
            )
            return url_for(endpoint, _external=True, **kwargs)
        return base + path
    return url_for(endpoint, _external=True, **kwargs)


def _require_subscriber(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_subscriber:
            flash("La connexion aux services cloud est réservée aux abonnés.", "warning")
            return redirect(url_for("user.dashboard"))
        return fn(*args, **kwargs)

    return wrapper


def _get_service(provider: str):
    if provider == "gdrive":
        from app.services import gdrive_service
        return gdrive_service
    if provider == "onedrive":
        from app.services import onedrive_service
        return onedrive_service
    if provider == "dropbox":
        from app.services import dropbox_service
        return dropbox_service
    if provider == "kdrive":
        from app.services import kdrive_service
        return kdrive_service
    if provider == "zotero":
        from app.services import zotero_service
        return zotero_service
    if provider == "s3":
        from app.services import s3_service
        return s3_service
    if provider == "notion":
        from app.services import notion_service
        return notion_service
    if provider == "webdav":
        from app.services import webdav_service
        return webdav_service
    return None


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

@user_cloud_bp.route("/cloud")
@login_required
@_require_subscriber
def cloud_connections():
    connections = CloudConnection.query.filter_by(user_id=current_user.id).all()
    # OSS single-user build: every account has full access to all connectors.
    user_plan = "pro"
    from app.utils.deployment_mode import is_sovereign
    return render_template(
        "user/cloud_connections.html",
        connections=connections,
        provider_labels=PROVIDER_LABELS,
        is_org_view=False,
        org=None,
        user_plan=user_plan,
        # In sovereign mode the template hides public-cloud buttons.
        # `allowed_providers=None` means "all providers allowed".
        allowed_providers=_SOVEREIGN_ALLOWED_PROVIDERS if is_sovereign() else None,
    )


# ---------------------------------------------------------------------------
# Generic OAuth connect / callback
# ---------------------------------------------------------------------------

# Sovereign air-gap deployments only allow connectors that can point to
# infrastructure the customer fully controls — typically an internal MinIO
# (S3-compatible) or an internal Nextcloud (WebDAV). Public cloud connectors
# (Google Drive / OneDrive / Dropbox / Notion / Zotero / kDrive) all rely on
# external HTTPS endpoints which a true air-gap cannot reach, so they're
# refused at the route level.
_SOVEREIGN_ALLOWED_PROVIDERS = {"s3", "webdav"}


def _provider_allowed_in_mode(provider: str) -> bool:
    """Check the deployment mode allows this connector at all."""
    from app.utils.deployment_mode import is_sovereign
    if is_sovereign():
        return provider in _SOVEREIGN_ALLOWED_PROVIDERS
    return True  # classic + hybrid allow everything


@user_cloud_bp.route("/cloud/<provider>/connect")
@login_required
@_require_subscriber
def cloud_connect(provider: str):
    if provider not in SUPPORTED_PROVIDERS:
        flash("Fournisseur cloud non supporté.", "danger")
        return redirect(url_for("user_cloud.cloud_connections"))

    if not _provider_allowed_in_mode(provider):
        flash(
            "Ce connecteur n'est pas disponible en mode souverain. "
            "Seuls S3 (vers MinIO interne) et WebDAV (vers Nextcloud interne) "
            "fonctionnent en air-gap.",
            "warning",
        )
        return redirect(url_for("user_cloud.cloud_connections"))

    if provider == "gdrive":
        return redirect(url_for("user_cloud.gdrive_connect_form"))

    if provider == "zotero":
        return redirect(url_for("user_cloud.zotero_connect_form"))

    if provider == "kdrive":
        return redirect(url_for("user_cloud.kdrive_connect_form"))

    if provider == "s3":
        return redirect(url_for("user_cloud.s3_connect_form"))

    if provider == "notion":
        return redirect(url_for("user_cloud.notion_connect_form"))

    if provider == "webdav":
        return redirect(url_for("user_cloud.webdav_connect_form"))

    svc = _get_service(provider)
    redirect_uri = _redirect_uri("user_cloud.cloud_callback", provider=provider)
    try:
        auth_url, state = svc.get_auth_url(redirect_uri)
    except Exception as e:
        logger.error(f"{provider} get_auth_url failed: {e}")
        flash(f"Impossible de démarrer la connexion {PROVIDER_LABELS.get(provider, provider)}.", "danger")
        return redirect(url_for("user_cloud.cloud_connections"))

    _store_oauth_state(provider, state, current_user.id)
    logger.debug("OAuth connect for %s (user %s): stored state=%s", provider, current_user.id, state)
    return redirect(auth_url)


# ---------------------------------------------------------------------------
# Google Drive — service account form (not OAuth)
# ---------------------------------------------------------------------------

@user_cloud_bp.route("/cloud/gdrive/connect", methods=["GET", "POST"])
@login_required
@_require_subscriber
def gdrive_connect_form():
    from app.services import gdrive_service

    if not _provider_allowed_in_mode("gdrive"):
        flash(
            "Ce connecteur n'est pas disponible en mode souverain.",
            "warning",
        )
        return redirect(url_for("user_cloud.cloud_connections"))

    service_account_email = gdrive_service.get_service_account_email()

    if request.method == "POST":
        folder_url = (request.form.get("folder_url") or "").strip()
        if not folder_url:
            flash("Veuillez entrer l'URL ou l'identifiant du dossier.", "danger")
            return render_template(
                "user/gdrive_connect.html",
                service_account_email=service_account_email,
            )

        folder_id = gdrive_service.extract_folder_id(folder_url)

        try:
            folder_meta = gdrive_service.validate_folder_access(folder_id)
        except RuntimeError as e:
            flash(str(e), "danger")
            return render_template(
                "user/gdrive_connect.html",
                service_account_email=service_account_email,
            )

        existing = CloudConnection.query.filter_by(
            user_id=current_user.id, provider="gdrive"
        ).first()
        conn = existing or CloudConnection(
            user_id=current_user.id, provider="gdrive", access_token_enc=""
        )
        if not existing:
            db.session.add(conn)

        conn.folder_id = folder_id
        conn.folder_name = folder_meta.get("name", "")
        conn.sync_cursor = None
        db.session.commit()

        flash(
            f"Google Drive connecté (dossier : {conn.folder_name}). "
            "La synchronisation démarrera automatiquement.",
            "success",
        )
        return redirect(url_for("user_cloud.cloud_connections"))

    return render_template(
        "user/gdrive_connect.html",
        service_account_email=service_account_email,
    )


# ---------------------------------------------------------------------------
# Zotero — API key form (not OAuth)
# ---------------------------------------------------------------------------

@user_cloud_bp.route("/cloud/zotero/connect", methods=["GET", "POST"])
@login_required
@_require_subscriber
def zotero_connect_form():
    from app.services import zotero_service

    if not _provider_allowed_in_mode("zotero"):
        from flask import abort
        abort(404)

    if request.method == "POST":
        api_key = (request.form.get("api_key") or "").strip()
        user_id = (request.form.get("user_id") or "").strip()

        if not api_key or not user_id:
            flash("Clé API et User ID Zotero requis.", "danger")
            return render_template("user/zotero_connect.html")

        existing = CloudConnection.query.filter_by(
            user_id=current_user.id, provider="zotero"
        ).first()
        conn = existing or CloudConnection(
            user_id=current_user.id, provider="zotero", access_token_enc=""
        )
        if not existing:
            db.session.add(conn)

        try:
            zotero_service.store_credentials(conn, api_key, user_id)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(f"Zotero store_credentials failed for user {current_user.id}: {e}")
            flash("Impossible de sauvegarder les identifiants Zotero.", "danger")
            return render_template("user/zotero_connect.html")

        flash("Zotero connecté. Vous pouvez maintenant lancer la synchronisation.", "success")
        return redirect(url_for("user_cloud.cloud_connections"))

    return render_template("user/zotero_connect.html")


# ---------------------------------------------------------------------------
# kDrive — API token form (not OAuth)
# ---------------------------------------------------------------------------

@user_cloud_bp.route("/cloud/kdrive/connect", methods=["GET", "POST"])
@login_required
@_require_subscriber
def kdrive_connect_form():
    from app.services import kdrive_service

    if not _provider_allowed_in_mode("kdrive"):
        from flask import abort
        abort(404)

    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        kdrive_id = (request.form.get("kdrive_id") or "").strip()
        # Infomaniak sometimes displays app passwords with spaces for readability.
        app_password = "".join((request.form.get("app_password") or "").split())

        form_data = {"email": email, "kdrive_id": kdrive_id}

        if not email or not app_password or not kdrive_id:
            flash("Tous les champs sont requis.", "danger")
            return render_template("user/kdrive_connect.html", **form_data)

        if not kdrive_id.isdigit():
            flash("Le kDrive ID doit être un nombre (ex. 123456).", "danger")
            return render_template("user/kdrive_connect.html", **form_data)

        cred_status = kdrive_service.check_credentials(email, app_password, kdrive_id)
        if cred_status == kdrive_service._CRED_NOT_FOUND:
            flash(
                f"kDrive ID « {kdrive_id} » introuvable. "
                "Vérifiez le nombre dans l'URL ksuite.infomaniak.com/kdrive/app/drive/<strong>ID</strong>/.",
                "danger",
            )
            return render_template("user/kdrive_connect.html", **form_data)
        if cred_status == kdrive_service._CRED_BAD_AUTH:
            flash(
                "E-mail ou mot de passe d'application incorrect. "
                "Vérifiez que l'adresse e-mail est bien celle de votre compte Infomaniak.",
                "danger",
            )
            return render_template("user/kdrive_connect.html", **form_data)
        if cred_status == kdrive_service._CRED_NO_ACCESS:
            flash(
                "Ce mot de passe d'application n'a pas accès à kDrive. "
                "Dans Manager Infomaniak → Sécurité → Mots de passe d'application, "
                "créez-en un nouveau en veillant à cocher l'accès kDrive.",
                "warning",
            )
            return render_template("user/kdrive_connect.html", **form_data)
        if cred_status == kdrive_service._CRED_ERROR:
            flash("Impossible de joindre le serveur Infomaniak. Réessayez.", "danger")
            return render_template("user/kdrive_connect.html", **form_data)

        existing = CloudConnection.query.filter_by(
            user_id=current_user.id, provider="kdrive"
        ).first()
        conn = existing or CloudConnection(
            user_id=current_user.id, provider="kdrive", access_token_enc=""
        )
        if not existing:
            db.session.add(conn)

        try:
            kdrive_service.store_credentials(conn, email, app_password, kdrive_id)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error("kDrive store_credentials failed for user %s: %s", current_user.id, e)
            flash("Impossible de sauvegarder les identifiants kDrive.", "danger")
            return render_template("user/kdrive_connect.html", **form_data)

        flash("kDrive connecté. Vous pouvez maintenant lancer la synchronisation.", "success")
        return redirect(url_for("user_cloud.cloud_connections"))

    return render_template("user/kdrive_connect.html")


# ---------------------------------------------------------------------------
# S3 / S3-compatible — credentials form (not OAuth)
# ---------------------------------------------------------------------------

@user_cloud_bp.route("/cloud/s3/connect", methods=["GET", "POST"])
@login_required
@_require_subscriber
def s3_connect_form():
    from app.services import s3_service

    if request.method == "POST":
        access_key = (request.form.get("access_key") or "").strip()
        secret_key = (request.form.get("secret_key") or "").strip()
        endpoint_url = (request.form.get("endpoint_url") or "").strip()
        bucket_name = (request.form.get("bucket_name") or "").strip()
        prefix = (request.form.get("prefix") or "").strip()
        display_name = (request.form.get("display_name") or "").strip() or "S3"

        form_data = {
            "access_key": access_key,
            "endpoint_url": endpoint_url,
            "bucket_name": bucket_name,
            "prefix": prefix,
            "display_name": display_name,
        }

        if not access_key or not secret_key or not bucket_name:
            flash("Access Key ID, Secret Access Key et nom du bucket sont requis.", "danger")
            return render_template("user/s3_connect.html", form_data=form_data)

        try:
            s3_service.test_connection(access_key, secret_key, endpoint_url or None, bucket_name)
        except Exception as e:
            logger.warning("S3 connection test failed for user %s: %s", current_user.id, e)
            flash(
                "Impossible de se connecter au bucket S3. "
                "Vérifiez vos identifiants, l'endpoint et le nom du bucket.",
                "danger",
            )
            return render_template("user/s3_connect.html", form_data=form_data)

        existing = CloudConnection.query.filter_by(
            user_id=current_user.id, provider="s3",
        ).first()
        conn = existing or CloudConnection(
            user_id=current_user.id,
            provider="s3",
            access_token_enc="",
        )
        if not existing:
            db.session.add(conn)

        try:
            s3_service.store_credentials(
                conn,
                access_key=access_key,
                secret_key=secret_key,
                endpoint_url=endpoint_url or "https://s3.amazonaws.com",
                bucket_name=bucket_name,
            )
            conn.folder_name = prefix or None
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error("S3 store_credentials failed for user %s: %s", current_user.id, e)
            flash("Impossible de sauvegarder les identifiants S3.", "danger")
            return render_template("user/s3_connect.html", form_data=form_data)

        flash("S3 connecté. Vous pouvez maintenant lancer la synchronisation.", "success")
        return redirect(url_for("user_cloud.cloud_connections"))

    return render_template("user/s3_connect.html", form_data=None)


# ---------------------------------------------------------------------------
# Notion — credentials form (token-based, no OAuth)
# ---------------------------------------------------------------------------

@user_cloud_bp.route("/cloud/notion/connect", methods=["GET", "POST"])
@login_required
@_require_subscriber
def notion_connect_form():
    from app.services import notion_service

    if not _provider_allowed_in_mode("notion"):
        from flask import abort
        abort(404)

    if request.method == "POST":
        token = (request.form.get("token") or "").strip()
        if not token:
            flash("Le token d'intégration est requis.", "danger")
            return render_template("user/notion_connect.html")

        if not notion_service.check_credentials(token):
            flash(
                "Token invalide ou intégration sans accès. "
                "Vérifiez le token et assurez-vous que des pages sont partagées avec l'intégration.",
                "danger",
            )
            return render_template("user/notion_connect.html")

        existing = CloudConnection.query.filter_by(
            user_id=current_user.id, provider="notion"
        ).first()
        conn = existing or CloudConnection(
            user_id=current_user.id, provider="notion", access_token_enc=""
        )
        if not existing:
            db.session.add(conn)

        try:
            notion_service.store_credentials(conn, token)
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.error("Notion store_credentials failed for user %s: %s", current_user.id, exc)
            flash("Impossible de sauvegarder le token Notion.", "danger")
            return render_template("user/notion_connect.html")

        flash("Notion connecté. Lancez la synchronisation pour indexer vos pages.", "success")
        return redirect(url_for("user_cloud.cloud_connections"))

    return render_template("user/notion_connect.html")


# ---------------------------------------------------------------------------
# WebDAV — credentials form (Basic Auth)
# ---------------------------------------------------------------------------

@user_cloud_bp.route("/cloud/webdav/connect", methods=["GET", "POST"])
@login_required
@_require_subscriber
def webdav_connect_form():
    from app.services import webdav_service

    if request.method == "POST":
        url = (request.form.get("url") or "").strip()
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        form_data = {"url": url, "username": username}

        if not url or not username or not password:
            flash("URL, identifiant et mot de passe sont requis.", "danger")
            return render_template("user/webdav_connect.html", form_data=form_data)

        status = webdav_service.check_credentials(url, username, password)
        if status == "bad_auth":
            flash("Identifiant ou mot de passe incorrect.", "danger")
            return render_template("user/webdav_connect.html", form_data=form_data)
        if status != "ok":
            flash("Impossible de joindre le serveur WebDAV. Vérifiez l'URL.", "danger")
            return render_template("user/webdav_connect.html", form_data=form_data)

        existing = CloudConnection.query.filter_by(
            user_id=current_user.id, provider="webdav"
        ).first()
        conn = existing or CloudConnection(
            user_id=current_user.id, provider="webdav", access_token_enc=""
        )
        if not existing:
            db.session.add(conn)

        try:
            webdav_service.store_credentials(conn, url, username, password)
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.error("WebDAV store_credentials failed for user %s: %s", current_user.id, exc)
            flash("Impossible de sauvegarder les identifiants WebDAV.", "danger")
            return render_template("user/webdav_connect.html", form_data=form_data)

        flash("WebDAV connecté. Lancez la synchronisation pour indexer vos fichiers.", "success")
        return redirect(url_for("user_cloud.cloud_connections"))

    return render_template("user/webdav_connect.html", form_data=None)


@user_cloud_bp.route("/cloud/<provider>/callback")
@login_required
@_require_subscriber
def cloud_callback(provider: str):
    if provider not in SUPPORTED_PROVIDERS:
        flash("Fournisseur cloud non supporté.", "danger")
        return redirect(url_for("user_cloud.cloud_connections"))

    code = request.args.get("code")
    state = request.args.get("state")

    logger.debug(
        "OAuth callback for %s: code=%s, state=%s, user=%s, session_keys=%s",
        provider, bool(code), bool(state), current_user.id, list(session.keys()),
    )

    if not code or not state or not _pop_oauth_state(provider, state, current_user.id):
        logger.warning(
            "OAuth state mismatch/missing for %s (user %s): code=%s state=%s",
            provider, current_user.id, bool(code), bool(state),
        )
        flash("Connexion annulée ou token invalide.", "warning")
        return redirect(url_for("user_cloud.cloud_connections"))

    svc = _get_service(provider)
    redirect_uri = _redirect_uri("user_cloud.cloud_callback", provider=provider)

    # On garde le SELECT … FOR UPDATE sur la ligne user pour sérialiser deux
    # callbacks OAuth parallèles du même user et éviter un double insert de la
    # même connexion provider.
    from app.models.user import User as _User
    db.session.query(_User).filter_by(id=current_user.id).with_for_update().first()
    existing = CloudConnection.query.filter_by(
        user_id=current_user.id, provider=provider
    ).first()

    conn = existing or CloudConnection(
        user_id=current_user.id, provider=provider, access_token_enc=""
    )
    if not existing:
        db.session.add(conn)

    try:
        svc.store_tokens(conn, code, redirect_uri)
    except Exception as e:
        logger.error(f"{provider} OAuth exchange failed for user {current_user.id}: {e}")
        flash(f"Erreur lors de la connexion à {PROVIDER_LABELS.get(provider, provider)}.", "danger")
        return redirect(url_for("user_cloud.cloud_connections"))

    conn.sync_cursor = None
    db.session.commit()

    label = PROVIDER_LABELS.get(provider, provider)
    flash(f"{label} connecté. Choisissez un dossier ou lancez la synchronisation.", "success")
    return redirect(url_for("user_cloud.folder_picker", connection_id=conn.id))


# ---------------------------------------------------------------------------
# Folder picker
# ---------------------------------------------------------------------------

@user_cloud_bp.route("/cloud/<int:connection_id>/folders")
@login_required
@_require_subscriber
def folder_picker(connection_id: int):
    conn = db.session.get(CloudConnection, connection_id)
    if not conn or conn.user_id != current_user.id:
        flash("Connexion introuvable.", "danger")
        return redirect(url_for("user_cloud.cloud_connections"))

    svc = _get_service(conn.provider)
    try:
        folders = svc.list_folders(conn)
    except Exception as e:
        logger.error(f"list_folders failed for connection {connection_id}: {e}")
        folders = []

    # For Google Drive (service-account model) the picker tells the user which
    # SA email to share their folder with — only shared folders are visible.
    sa_email = None
    if conn.provider == "gdrive":
        try:
            sa_email = svc.get_service_account_email()
        except Exception:
            sa_email = None

    return render_template(
        "user/cloud_folder_picker.html",
        conn=conn,
        folders=folders,
        sa_email=sa_email,
    )


@user_cloud_bp.route("/cloud/<int:connection_id>/set-folder", methods=["POST"])
@login_required
@_require_subscriber
def set_folder(connection_id: int):
    conn = db.session.get(CloudConnection, connection_id)
    if not conn or conn.user_id != current_user.id:
        return jsonify({"error": "Not found"}), 404

    # Bug H15 — request.json crashe en AttributeError/415 si content-type
    # n'est pas application/json ou si le body est vide. Fix : silent=True
    # + dict fallback + bornes sur les valeurs.
    data = request.get_json(silent=True) or {}
    folder_id = data.get("folder_id")
    folder_name = data.get("folder_name")
    if folder_id is not None and not isinstance(folder_id, str):
        return jsonify({"error": "folder_id must be a string"}), 400
    if folder_name is not None and not isinstance(folder_name, str):
        return jsonify({"error": "folder_name must be a string"}), 400
    folder_id = (folder_id or "")[:512] or None
    # Bug N7 (audit round 2) — gdrive expose validate_folder_access pour
    # vérifier que le folder existe + appartient au compte service. On
    # l'applique ici aussi (idempotent, déjà cache côté service) pour
    # éviter de stocker un folder_id qui fera planter le sync au tick
    # suivant. Pour les autres providers, le scope OAuth limite le risque
    # mais on garde la longueur cap (déjà fait).
    if folder_id and conn.provider == "gdrive":
        try:
            from app.services import gdrive_service
            gdrive_service.validate_folder_access(folder_id)
        except Exception as e:
            logger.warning("set_folder: gdrive folder %s rejected for conn %s: %s",
                           folder_id, conn.id, e)
            return jsonify({"error": "Folder not accessible"}), 400
    conn.folder_id = folder_id
    conn.folder_name = (folder_name or "")[:255] or None
    conn.sync_cursor = None
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Manual sync
# ---------------------------------------------------------------------------

@user_cloud_bp.route("/cloud/<int:connection_id>/sync", methods=["POST"])
@login_required
@_require_subscriber
def trigger_sync(connection_id: int):
    from datetime import datetime, timezone

    from app.cron.check_cloud_sync import _effective_min_minutes

    conn = db.session.get(CloudConnection, connection_id)
    if not conn or conn.user_id != current_user.id:
        return jsonify({"error": "Not found"}), 404

    if conn.is_syncing:
        return jsonify({"error": "Synchronisation déjà en cours"}), 409

    # Honour the same per-tier floor the cron enforces. Without this, a
    # Standard user could bypass the 24 h cap by spamming the "Sync now"
    # button — which defeats the cost-control purpose of the limit.
    if conn.last_sync_at is not None:
        last = conn.last_sync_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
        floor_min = _effective_min_minutes(conn)
        if elapsed_min < floor_min:
            wait_min = int(floor_min - elapsed_min)
            return jsonify({
                "error": "Cadence de synchronisation atteinte pour votre plan",
                "retry_after_minutes": wait_min,
            }), 429

    svc = _get_service(conn.provider)
    app = current_app._get_current_object()

    def _run():
        # Bug M14 — rollback + remove explicites en defense-in-depth.
        with app.app_context():
            try:
                svc.sync_connection(connection_id)
            except Exception:
                db.session.rollback()
                logger.exception("Manual sync failed for connection %s", connection_id)
            finally:
                db.session.remove()

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "Synchronisation lancée"})


# ---------------------------------------------------------------------------
# Sync progress (polled by the dashboard banner)
# ---------------------------------------------------------------------------

@user_cloud_bp.route("/cloud/sync-status", methods=["GET"])
@login_required
def sync_status():
    """Return JSON describing in-flight cloud syncs for the current user.

    Polled every ~10 s by the banner partial in base.html. Returns an empty
    list when nothing is running so the banner removes itself.

    Response shape::

        {"syncs": [
            {"id": 1, "provider": "gdrive", "provider_label": "Google Drive",
             "processed": 87, "total": 520, "started_at": "...",
             "elapsed_s": 152, "eta_s": 752}
        ]}
    """
    from datetime import datetime, timezone

    rows = (
        db.session.query(CloudConnection)
        .filter(CloudConnection.user_id == current_user.id,
                CloudConnection.is_syncing.is_(True))
        .all()
    )

    payload = []
    now = datetime.now(timezone.utc)
    for c in rows:
        processed = c.sync_processed_files or 0
        total = c.sync_total_files
        started = c.sync_started_at
        if started and started.tzinfo is None:
            started_aware = started.replace(tzinfo=timezone.utc)
        else:
            started_aware = started
        elapsed_s = int((now - started_aware).total_seconds()) if started_aware else None

        # ETA: derive from observed throughput. Skip on the first few items
        # where the rate isn't yet representative — banner just shows "…".
        eta_s = None
        if processed >= 3 and total and elapsed_s and elapsed_s > 0:
            rate = processed / elapsed_s
            if rate > 0:
                remaining = max(0, total - processed)
                eta_s = int(remaining / rate)

        payload.append({
            "id": c.id,
            "provider": c.provider,
            "provider_label": c.provider_label,
            "processed": processed,
            "total": total,
            "started_at": started_aware.isoformat() if started_aware else None,
            "elapsed_s": elapsed_s,
            "eta_s": eta_s,
        })

    return jsonify({"syncs": payload})


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------

@user_cloud_bp.route("/cloud/<int:connection_id>/disconnect", methods=["POST"])
@login_required
@_require_subscriber
def disconnect(connection_id: int):
    from app.services.rag_service import remove_cloud_document_from_index

    conn = db.session.get(CloudConnection, connection_id)
    if not conn or conn.user_id != current_user.id:
        flash("Connexion introuvable.", "danger")
        return redirect(url_for("user_cloud.cloud_connections"))

    for doc in conn.documents.all():
        if doc.chunk_count > 0:
            try:
                remove_cloud_document_from_index(current_user.id, doc.id, doc.chunk_count)
            except Exception as e:
                logger.error(f"Failed to remove cloud doc {doc.id} from index: {e}")

    label = conn.provider_label
    db.session.delete(conn)
    db.session.commit()
    flash(f"{label} déconnecté et documents supprimés de l'index.", "success")
    return redirect(url_for("user_cloud.cloud_connections"))


# ---------------------------------------------------------------------------
# Sync frequency
# ---------------------------------------------------------------------------

_FREQUENCY_OPTIONS = {
    # Syncs can be brought as close as hourly (60 min floor).
    "pro": {60, 240, 720, 1440},
}


@user_cloud_bp.route("/cloud/<int:connection_id>/set-frequency", methods=["POST"])
@login_required
@_require_subscriber
def set_sync_frequency(connection_id: int):
    """Update the sync frequency for a personal cloud connection (Pro+).

    Pivot Free/Pro/Enterprise : Free est sur une cadence fixe (24 h). Pro peut
    rapprocher les syncs jusqu'à toutes les heures (plancher 60 min). Enterprise
    descend jusqu'à 15 min.
    """
    conn = db.session.get(CloudConnection, connection_id)
    if not conn or conn.user_id != current_user.id:
        return jsonify({"error": "Not found"}), 404

    allowed = _FREQUENCY_OPTIONS["pro"]

    data = request.get_json(silent=True) or {}
    try:
        minutes = int(data.get("minutes", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid value"}), 400

    if minutes not in allowed:
        return jsonify({"error": "Invalid frequency for your plan"}), 400

    conn.sync_frequency_minutes = minutes
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error("set_sync_frequency failed for connection %d: %s", connection_id, e)
        return jsonify({"error": "Database error"}), 500

    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Document list (AJAX)
# ---------------------------------------------------------------------------

@user_cloud_bp.route("/cloud/<int:connection_id>/documents")
@login_required
@_require_subscriber
def list_documents(connection_id: int):
    conn = db.session.get(CloudConnection, connection_id)
    if not conn or conn.user_id != current_user.id:
        return jsonify({"error": "Not found"}), 404

    docs = (
        CloudDocument.query.filter_by(connection_id=connection_id)
        .order_by(CloudDocument.name)
        .all()
    )
    return jsonify([
        {
            "id": d.id,
            "name": d.name,
            "mime_type": d.mime_type,
            "file_size": d.file_size,
            "chunk_count": d.chunk_count,
            "indexed_at": d.indexed_at.isoformat() if d.indexed_at else None,
            "tags": [{"id": t.id, "name": t.name} for t in d.tags],
        }
        for d in docs
    ])
