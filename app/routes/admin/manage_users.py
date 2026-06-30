from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required
from requests.exceptions import ConnectionError, Timeout

from app.ext import db
from app.models.user import User
from app.routes.admin.admin import admin_bp, admin_required
from app.utils.audit import log_audit
from app.utils.logger import get_logger
from app.utils.mailer import send_email_via_brevo_api
from app.utils.password_utils import generate_strong_password
from app.utils.tokens import generate_confirmation_token


manage_users_bp = Blueprint("manage_users", __name__)

logger = get_logger("manage_users", subfolder="routes")


# Route : Création manuelle d’un utilisateur (admin)
@admin_bp.route("/create_user", methods=["POST"])
@login_required
@admin_required
def create_user():
    """Create a new user from admin form data.

    Returns:
        str: Redirect to manage users page
    """
    email = request.form.get("email", "").strip().lower()
    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    is_admin = bool(request.form.get("is_admin"))
    plan = request.form.get("plan", "premium_pro")
    if plan not in ("premium_pro", "enterprise"):
        plan = "premium_pro"

    # Validation rapide
    if not email:
        flash("Email requis.", "danger")
        return redirect(url_for("admin.manage_users"))
    if User.query.filter_by(email=email).first():
        flash("Un utilisateur avec cet email existe déjà.", "warning")
        return redirect(url_for("admin.manage_users"))

    # Génère un mot de passe temporaire respectant la politique de sécurité
    temp_password = generate_strong_password()
    new_user = User(
        email=email,
        firstname=first_name,
        lastname=last_name,
        is_admin=is_admin,
        is_active=True,
        is_confirmed=True,
        abonnement_type=plan,
    )
    new_user.set_password(temp_password)
    try:
        db.session.add(new_user)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f"Erreur lors de la création de l'utilisateur: {e}")
        flash("Erreur lors de la création de l'utilisateur.", "danger")
        return redirect(url_for("admin.manage_users"))

    login_url = url_for("auth.login", _external=True)
    # Envoi du mot de passe provisoire par email
    html_body = render_template(
        "emails/new_user_temp_password.html",
        first_name=first_name or "",
        temp_password=temp_password,
        login_url=login_url,
    )
    try:
        send_email_via_brevo_api(
            to=email,
            subject="👋 Bienvenue sur Myeline – Votre accès",
            html_content=html_body,
        )
        flash(f"✅ Utilisateur {email} créé et email envoyé.", "success")
    except (ConnectionError, Timeout):
        logger.warning(
            "Échec de l'envoi de l'email de création à %s",
            email, exc_info=True,
        )
        flash("Utilisateur créé, mais l'email de notification n'a pas pu être envoyé.", "warning")
    except Exception as e:
        logger.error(
            f"Erreur inattendue lors de l'envoi de l'email de création à {email} : "
            f"{e}",
            exc_info=True,
        )
        flash("Utilisateur créé mais erreur envoi email.", "warning")
    return redirect(url_for("admin.manage_users"))


@admin_bp.route("/delete_user/<int:user_id>", methods=["POST"])
@login_required
@admin_required
def delete_user(user_id):
    """Delete a user by ID.

    Args:
        user_id (int): ID of user to delete

    Returns:
        str: Redirect to manage users page
    """
    user = db.get_or_404(User, user_id)
    # Snapshot identifying fields BEFORE delete — once the row is
    # removed the audit payload couldn't reconstruct them.
    deleted_email = user.email
    try:
        log_audit(
            "user.delete",
            target_type="user", target_id=user_id,
            payload={"email": deleted_email},
        )
        # Bug CC5 — scrub audit trail AVANT delete (Art 17).
        # log_audit() au-dessus snapshote l'email actor (admin), pas la
        # victime — donc le scrub ci-dessous n'écrase pas la trace de
        # qui a fait l'action, juste les anciennes lignes où l'user
        # supprimé était lui-même actor.
        from app.utils.gdpr import scrub_user_audit_trail
        scrub_user_audit_trail(user, reason="admin_delete")
        db.session.delete(user)
        db.session.commit()
        flash(f"L'utilisateur {deleted_email} a été supprimé.", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(
            f"Erreur lors de la suppression de l'utilisateur {deleted_email}: {e}"
        )
        flash("Erreur lors de la suppression de l'utilisateur.", "danger")
    return redirect(url_for("admin.manage_users"))


@admin_bp.route("/user/<int:user_id>/set-plan", methods=["POST"])
@login_required
@admin_required
def set_user_plan(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("Utilisateur introuvable.", "danger")
        return redirect(url_for("admin.manage_users"))

    plan = request.form.get("plan", "premium_pro")
    if plan not in ("premium_pro", "enterprise"):
        plan = "premium_pro"

    previous_plan = user.abonnement_type
    user.abonnement_type = plan

    try:
        log_audit(
            "user.set_plan",
            target_type="user", target_id=user_id,
            payload={
                "from": previous_plan, "to": plan,
                "by_admin": True,
            },
        )
        db.session.commit()
        flash(f"Plan mis à jour pour {user.email}.", "success")
    except Exception as e:
        db.session.rollback()
        logger.error(f"Erreur set_user_plan user_id={user_id}: {e}")
        flash("Erreur lors de la mise à jour du plan.", "danger")
    return redirect(url_for("admin.manage_users"))


@admin_bp.route("/user/<int:user_id>/edit-name", methods=["POST"])
@login_required
@admin_required
def edit_user_name(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("Utilisateur introuvable.", "danger")
        return redirect(url_for("admin.manage_users"))
    old_first, old_last = user.firstname, user.lastname
    user.firstname = (request.form.get("firstname") or "").strip() or None
    user.lastname = (request.form.get("lastname") or "").strip() or None
    log_audit(
        "user.edit_name",
        target_type="user", target_id=user_id,
        payload={"email": user.email,
                 "from": {"firstname": old_first, "lastname": old_last},
                 "to": {"firstname": user.firstname, "lastname": user.lastname}},
    )
    db.session.commit()
    flash(f"Informations mises à jour pour {user.email}.", "success")
    return redirect(url_for("admin.manage_users"))


@admin_bp.route("/user/<int:user_id>/reset-2fa", methods=["POST"])
@login_required
@admin_required
def reset_user_2fa(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("Utilisateur introuvable.", "danger")
        return redirect(url_for("admin.manage_users"))
    previous_method = user.twofa_method
    user.twofa_method = None
    user.totp_secret = None
    log_audit(
        "user.reset_2fa",
        target_type="user", target_id=user_id,
        payload={"email": user.email, "previous_method": previous_method},
    )
    db.session.commit()
    logger.info("Admin reset 2FA for user_id=%s", user_id)
    flash(f"2FA réinitialisée pour {user.email}.", "success")
    return redirect(url_for("admin.manage_users"))


@admin_bp.route("/user/<int:user_id>/toggle-stats-exclusion", methods=["POST"])
@login_required
@admin_required
def toggle_stats_exclusion(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("Utilisateur introuvable.", "danger")
        return redirect(url_for("admin.manage_users"))
    user.exclude_from_stats = not user.exclude_from_stats
    log_audit(
        "user.toggle_stats_exclusion",
        target_type="user", target_id=user_id,
        payload={"email": user.email, "excluded": user.exclude_from_stats},
    )
    db.session.commit()
    state = "exclu" if user.exclude_from_stats else "inclus"
    logger.info("Admin toggled stats exclusion for user_id=%s → %s", user_id, state)
    flash(f"{user.email} est désormais {state} des statistiques.", "success")
    return redirect(url_for("admin.manage_users"))


@admin_bp.route("/user/<int:user_id>/edit-email", methods=["POST"])
@login_required
@admin_required
def edit_user_email(user_id):
    user = db.get_or_404(User, user_id)
    new_email = (request.form.get("email") or "").strip().lower()
    if not new_email:
        flash("Adresse email invalide", "danger")
        return redirect(url_for("admin.manage_users"))

    old_email = user.email
    user.email = new_email
    user.is_confirmed = False
    token = generate_confirmation_token(new_email)
    user.confirmation_token = token
    log_audit(
        "user.edit_email",
        target_type="user", target_id=user_id,
        payload={"from": old_email, "to": new_email},
    )
    db.session.commit()

    confirm_url = url_for("auth.confirm_email", token=token, _external=True)
    send_email_via_brevo_api(
        to=new_email,
        subject="Confirmation de votre compte MGnews",
        html_content=render_template(
            "emails/email_welcome.html", user=user, confirm_url=confirm_url
        ),
    )

    flash(
        f"L’adresse email a été modifiée et un mail de confirmation a été envoyé à {new_email}.",
        "success",
    )
    return redirect(url_for("admin.manage_users"))
