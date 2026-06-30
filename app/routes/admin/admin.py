import secrets
from functools import wraps
from pathlib import Path

import requests
from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.forms.admin_forms import AdminLoginForm
from app.models.user import User
from app.utils.flask_limiter import conditional_limit
from app.utils.logger import get_logger
from app.utils.mailer import send_email_via_brevo_api
from app.utils.time_utils import utcnow


_2FA_TTL_SECONDS = 600

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
MODULES_DIR = str(_ROOT / "workers")
SRC_DIR = str(_ROOT / "scrappers")
LOGS_DIR = str(_ROOT / "logs")
REPORTS_DIR = str(_ROOT / "data" / "raw")

admin_bp = Blueprint("admin", __name__)

logger = get_logger("admin", subfolder="routes")

# Decorator to require admin access


def admin_required(f):
    """Decorator to require admin authentication for routes.

    Args:
        f: Function to decorate

    Returns:
        function: Decorated function that requires admin access
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            logger.error("Accès réservé à l'administrateur")
            flash("Accès réservé à l'administrateur", "danger")
            return redirect(url_for("admin.admin_login"))
        return f(*args, **kwargs)

    return decorated_function


def _issue_admin_otp(user) -> None:
    """Generate, store in session, and email an admin OTP."""
    code = "{:06d}".format(secrets.randbelow(1_000_000))
    session["admin_2fa_code"] = code
    session["admin_2fa_email"] = user.email
    session["admin_2fa_pending_id"] = user.id
    session["admin_2fa_issued_at"] = utcnow().timestamp()
    from flask import current_app
    if current_app.config.get("FLASK_ENV") != "production":
        logger.warning("DEV/FALLBACK — Admin 2FA code for user_id=%s : %s", user.id, code)
    try:
        send_email_via_brevo_api(
            to=user.email,
            subject="🔐 Votre code de vérification admin Myeline",
            html_content=(
                f"<p>Votre code de connexion admin Myeline :</p>"
                f"<p><strong style='font-size:2em;letter-spacing:.2em'>{code}</strong></p>"
                f"<p style='color:#888;font-size:.85rem;'>Valable 10 minutes.</p>"
            ),
        )
        logger.info("OTP admin envoyé par email user_id=%s", user.id)
    except Exception as exc:
        logger.error("Échec envoi OTP admin user_id=%s : %s — code disponible dans les logs", user.id, exc)


@admin_bp.route("/login", methods=["GET", "POST"])
@conditional_limit("5/minute")
def admin_login():
    if current_user.is_authenticated:
        return redirect(url_for("admin.dashboard"))

    # Path: password already verified by /login, skip credentials form.
    pre_verified_id = session.pop("admin_pwd_verified", None)
    if pre_verified_id:
        from app.ext import db as _db
        user = _db.session.get(User, pre_verified_id)
        if user and user.is_admin:
            session["admin_2fa_pending_id"] = user.id
            if user.twofa_method == "totp" and user.totp_secret:
                return redirect(url_for("admin.admin_otp"))
            _issue_admin_otp(user)
            flash("Un code de vérification a été envoyé à votre adresse email.", "info")
            return redirect(url_for("admin.admin_otp"))

    form = AdminLoginForm()
    if form.validate_on_submit():
        email = form.email.data
        user = User.query.filter_by(email=email).first()
        if user and user.is_admin and user.check_password(form.password.data):
            logger.debug("Connexion admin credentials OK user_id=%s", user.id)
            session["admin_2fa_pending_id"] = user.id
            if user.twofa_method == "totp" and user.totp_secret:
                return redirect(url_for("admin.admin_otp"))
            _issue_admin_otp(user)
            flash("Un code de vérification a été envoyé à votre adresse email.", "info")
            return redirect(url_for("admin.admin_otp"))
        logger.warning("Connexion admin échouée")
        flash("Email ou mot de passe incorrect.", "danger")

    return render_template("log/login.html", form=form, is_admin=True)


@admin_bp.route("/otp", methods=["GET", "POST"])
@conditional_limit("10/minute")
def admin_otp():
    """Step 2 of admin login: TOTP or email OTP verification."""
    if current_user.is_authenticated:
        return redirect(url_for("admin.dashboard"))

    pending_id = session.get("admin_2fa_pending_id")
    if not pending_id:
        return redirect(url_for("admin.admin_login"))

    from app.ext import db as _db
    user = _db.session.get(User, pending_id)
    if not user or not user.is_admin:
        session.pop("admin_2fa_pending_id", None)
        return redirect(url_for("admin.admin_login"))

    use_totp = user.twofa_method == "totp" and user.totp_secret

    if request.method == "POST":
        submitted = request.form.get("otp_code", "").strip()

        if use_totp:
            import pyotp
            if pyotp.TOTP(user.totp_secret).verify(submitted, valid_window=1):
                return _complete_admin_login(user)
            flash("Code invalide. Vérifiez l'heure de votre appareil.", "danger")
        else:
            if not session.get("admin_2fa_code"):
                flash("Session expirée. Recommencez la connexion.", "warning")
                return redirect(url_for("admin.admin_login"))
            now_ts = utcnow().timestamp()
            if now_ts - session.get("admin_2fa_issued_at", 0) > _2FA_TTL_SECONDS:
                session.pop("admin_2fa_code", None)
                flash("Code expiré. Recommencez la connexion.", "warning")
                return redirect(url_for("admin.admin_login"))
            # Audit round 4 — A4-M1. Pop the code from the session BEFORE the
            # constant-time comparison so a successful verification never
            # leaves the OTP readable to a concurrent request (or anyone who
            # later inspects the session cookie). Failed comparisons still
            # consume the code — the admin has to restart the login flow,
            # which mirrors the user 2FA behaviour and prevents an attacker
            # with the email content from brute-forcing the 6 digits.
            #
            # Bug M5 (audit round 2) — l'ancien `==` n'est pas constant-time.
            # Code 6 digits, timing channel négligeable mais incohérent
            # avec user_2fa qui utilise compare_digest (cohérence + bonne
            # pratique).
            import hmac as _hmac
            expected = session.pop("admin_2fa_code") or ""
            if _hmac.compare_digest(submitted, expected):
                return _complete_admin_login(user)
            flash("Code incorrect.", "danger")
            return redirect(url_for("admin.admin_login"))

    hint = session.get("admin_2fa_email", user.email)
    from flask import current_app
    dev_code = (
        session.get("admin_2fa_code")
        if not use_totp and current_app.config.get("FLASK_ENV") != "production"
        else None
    )
    return render_template("log/admin_otp.html", hint=hint, dev_code=dev_code, use_totp=use_totp)


def _complete_admin_login(user):
    for k in ("admin_2fa_code", "admin_2fa_email", "admin_2fa_issued_at", "admin_2fa_pending_id"):
        session.pop(k, None)
    # Session fixation defence — clear before login_user (audit H-7).
    session.clear()
    login_user(user)
    logger.info("Connexion admin 2FA réussie user_id=%s", user.id)
    flash("Connexion réussie", "success")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/logout")
@login_required
def logout():
    """Handle admin logout.

    Returns:
        str: Redirect to welcome page after logout
    """
    logger.info(f"Déconnexion de l'admin (user_id={getattr(current_user, 'id', None)})")
    session.clear()
    logout_user()
    flash("Déconnexion réussie", "info")
    return redirect(url_for("home.welcome_page"))


@admin_bp.route("/dashboard")
@login_required
@admin_required
def dashboard():
    """Display admin dashboard with summary data.

    Returns:
        str: Rendered admin dashboard template
    """
    # Get health check data
    try:
        health_url = request.host_url.rstrip("/") + "/health"
        health_response = requests.get(health_url, timeout=2)
        health_data = (
            health_response.json() if health_response.status_code == 200 else None
        )
    except Exception:
        health_data = None

    # RAG feedback stats
    try:
        from datetime import timedelta

        from app.ext import db as _db
        from app.models.rag_feedback import RagFeedback
        from app.utils.time_utils import utcnow
        _since = utcnow() - timedelta(days=30)
        # RagFeedback has a `query` column that shadows Model.query —
        # dispatch via db.session.query() so SQLAlchemy uses the session
        # rather than the encrypted column descriptor.
        feedback_total   = _db.session.query(RagFeedback).filter(RagFeedback.created_at >= _since).count()
        feedback_up      = _db.session.query(RagFeedback).filter(RagFeedback.created_at >= _since, RagFeedback.rating == "thumbs_up").count()
        feedback_down    = _db.session.query(RagFeedback).filter(RagFeedback.created_at >= _since, RagFeedback.rating == "thumbs_down").count()
        feedback_recent  = (
            _db.session.query(RagFeedback)
            .order_by(RagFeedback.created_at.desc())
            .limit(20)
            .all()
        )
        feedback_stats = {
            "total": feedback_total,
            "up": feedback_up,
            "down": feedback_down,
            "satisfaction": round(feedback_up / feedback_total * 100) if feedback_total else None,
            "recent": feedback_recent,
        }
    except Exception:
        feedback_stats = None

    return render_template(
        "admin/dashboard.html",
        health_data=health_data,
        feedback_stats=feedback_stats,
    )


# Route pour afficher et gérer les abonnés


@admin_bp.route("/manage_users", methods=["GET"])
@login_required
@admin_required
def manage_users():
    search = request.args.get("search", "", type=str)
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 10, type=int)
    if per_page not in (10, 50, 100):
        per_page = 10
    query = User.query
    if search:
        query = query.filter(User.email.ilike(f"%{search}%"))
    pagination = query.order_by(User.email.asc()).paginate(page=page, per_page=per_page)
    users = pagination.items

    return render_template(
        "admin/manage_users.html",
        users=users,
        pagination=pagination,
        search=search,
        per_page=per_page,
    )


# Tags listing moved to tags_bp
# Cron routes moved to admin_cron.py
# Tools/RAG routes moved to admin_ops.py
