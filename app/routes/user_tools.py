import json

from flask import Blueprint, current_app, flash, jsonify, make_response, redirect, request, url_for
from flask_login import current_user, login_required, logout_user
from itsdangerous import BadData, BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.ext import db
from app.models import User
from app.utils.flask_limiter import conditional_limit
from app.utils.logger import get_logger
from app.utils.mailer import send_email_via_brevo_api
from app.utils.time_utils import utcnow


logger = get_logger("user_tools", subfolder="routes")
user_tools_bp = Blueprint("user_tools", __name__)


@user_tools_bp.route("/unsubscribe/<token>")
def unsubscribe(token):
    # Bug C4 — ce code utilisait URLSafeSerializer (non-timed) avec
    # max_age en argument, qui était silencieusement ignoré → token
    # éternel ou TypeError selon la version. URLSafeTimedSerializer.loads
    # respecte bien max_age. Doit matcher le serializer côté génération
    # (services/user_services.py:53).
    s = URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="unsubscribe")
    try:
        logger.debug("Tentative de désabonnement")
        data = s.loads(token, max_age=60 * 60 * 24 * 7)  # lien valide 7 jours
        user = db.get_or_404(User, data["user_id"])
        # Bug N12 (audit round 2) — l'ancien code posait `is_active=False`
        # ce qui DÉSACTIVAIT le compte entier (Flask-Login bloque les
        # inactifs au login). L'user voulait juste arrêter les emails.
        # On set newsletter_frequency à "none" — le compte reste utilisable.
        user.newsletter_frequency = "none"
        try:
            db.session.commit()
            flash(
                "Vous ne recevrez plus les emails Myeline. "
                "Votre compte reste actif — vous pouvez vous reconnecter à tout moment.",
                "info",
            )
        except Exception:
            db.session.rollback()
            logger.error("Erreur lors du désabonnement")
            flash("Erreur lors du désabonnement.", "danger")
    except (SignatureExpired, BadSignature, BadData):
        logger.warning("Échec de désabonnement : token invalide ou expiré")
        flash("Lien de désabonnement invalide ou expiré.", "danger")
    except Exception:
        logger.error("Erreur inattendue lors du désabonnement")
        flash("Erreur lors du désabonnement.", "danger")
    return redirect(url_for("auth.login"))


@user_tools_bp.route("/update-newsletter-frequency", methods=["POST"])
@login_required
def update_newsletter_frequency():
    data = request.get_json(silent=True) or {}
    freq = (data.get("frequency") or "none").strip()
    if freq not in ("none", "hourly", "daily", "weekly", "monthly"):
        return jsonify({"success": False, "error": "Fréquence invalide."}), 400
    # Pivot Free/Pro/Enterprise digest cadences:
    #   Free (gratuit)  → "none" ou "monthly" uniquement
    #   Pro / Enterprise / org / admin → toutes les cadences
    # (is_pro_subscriber est True pour admin, org member, premium_pro,
    # enterprise — voir User.is_pro_subscriber.)
    if current_user.is_pro_subscriber:
        allowed = ("none", "hourly", "daily", "weekly", "monthly")
    else:
        allowed = ("none", "monthly")
    if freq not in allowed:
        return jsonify({
            "success": False,
            "error": "Cette cadence est réservée au plan Pro. Le plan Free permet le récap mensuel.",
        }), 403
    current_user.newsletter_frequency = freq
    try:
        db.session.commit()
        logger.info("Fréquence récap mise à jour freq=%s user_id=%s", freq, current_user.id)
        return jsonify({"success": True})
    except Exception:
        db.session.rollback()
        logger.error("Erreur mise à jour fréquence récap user_id=%s", current_user.id)
        return jsonify({"success": False, "error": "Erreur lors de la mise à jour."}), 500


@user_tools_bp.route("/export-my-data", methods=["GET"])
@login_required
@conditional_limit("3 per day", key_func=lambda: f"export_data:{current_user.id}")
def export_my_data():
    """Full GDPR Article 15 + 20 export — every record we hold for the user.

    Returns a ZIP containing profile.json, documents (files + metadata),
    cloud_documents.json, scrapers + articles, conversations, query
    history, watch alerts, RAG feedback, tags, AND the user's ChromaDB
    chunks (text + metadata, no embeddings).
    """
    from flask import send_file

    from app.services.data_export import build_user_export

    user = current_user
    logger.info("Export complet demandé user_id=%s", user.id)
    buf = build_user_export(user)
    timestamp = utcnow().strftime("%Y%m%d_%H%M%S")
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"my_data_{user.id}_{timestamp}.zip",
    )


@user_tools_bp.route("/delete_account", methods=["POST"])
@login_required
def delete_account():
    user_id = current_user.id
    logger.debug(f"Requête de suppression de compte pour l'utilisateur ID {user_id}")
    user = db.session.get(User, user_id)

    # Bug L11 — l'ancienne route acceptait un POST simple : une session
    # compromise (XSS, cookie volé) suffisait pour déclencher la
    # suppression irréversible. Désormais on exige le mot de passe courant
    # pour confirmer l'intention de l'humain. Les comptes sans password
    # (SSO/OAuth-only) sont exemptés via has_usable_password.
    current_password = (request.form.get("current_password") or "").strip()
    if user.password_hash and not user.check_password(current_password):
        logger.warning("delete_account: mauvais mot de passe user_id=%s", user_id)
        flash("Mot de passe incorrect. Suppression annulée.", "danger")
        return redirect(url_for("user.account_settings"))

    try:
        logout_user()  # Déconnexion
        user_email = user.email
        logger.info("Utilisateur user_id=%s déconnecté avant suppression", user_id)

        # Copie infos avant suppression pour email
        user_firstname = user.firstname

        # Bug CC5 — scrub audit trail pour Art 17 (sinon actor_email
        # reste en clair dans audit_logs ad vitam).
        from app.utils.gdpr import scrub_user_audit_trail
        scrub_user_audit_trail(user, reason="self_delete")
        db.session.delete(user)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.error("Erreur lors de la suppression du compte")
            flash("Erreur lors de la suppression du compte.", "danger")
            return redirect(url_for("user.account_settings"))

        # Envoi email de confirmation de suppression
        # Bug N11 (audit round 2) — l'ancien code interpolait firstname
        # brut dans le HTML. Un user au firstname "<script>" ou
        # `"><img onerror=…>` pouvait injecter du markup chez les
        # clients mail rendant le HTML. Escape via markupsafe.
        from markupsafe import escape as _esc
        try:
            send_email_via_brevo_api(
                to=user_email,
                subject="👋 Votre compte Myeline a été supprimé",
                html_content=(
                    f"<p>Bonjour {_esc(user_firstname or '')},</p>"
                    "<p>Votre compte Myeline et votre abonnement ont bien été supprimés.</p>"
                    "<p>Nous vous remercions de nous avoir fait confiance.</p>"
                    "<p>À tout moment, vous pouvez recréer un compte si vous le souhaitez.</p>"
                    "<p>Bien à vous,<br>L’équipe Myeline</p>"
                ),
            )
        except (IOError, OSError):
            logger.error("Erreur envoi mail suppression compte user_id=%s", user_id)
        except Exception:
            logger.error("Erreur inattendue envoi mail suppression compte user_id=%s", user_id)

        flash("Votre compte (et abonnement) a été supprimé avec succès.", "info")
        return redirect(url_for("auth.login"))

    except (ValueError, KeyError):
        logger.error("Erreur de données lors de la suppression du compte user_id=%s", user_id)
        flash("Erreur de données lors de la suppression du compte.", "danger")
        return redirect(url_for("user.account_settings"))
    except Exception:
        logger.error("Erreur inattendue lors de la suppression du compte user_id=%s", user_id)
        flash("Une erreur est survenue pendant la suppression du compte.", "danger")
        return redirect(url_for("user.account_settings"))
