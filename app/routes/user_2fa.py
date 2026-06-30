import base64
import hmac
import io
import os
import secrets
import time

import pyotp
import qrcode
from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required

from app.ext import db, limiter
from app.utils.logger import get_logger
from app.utils.mailer import send_email_via_brevo_api


logger = get_logger("user_2fa", subfolder="routes")

user_2fa_bp = Blueprint("user_2fa", __name__)

_ISSUER = "Myeline"
_OTP_TTL = 600  # 10 min


def _conditional_rate_limit(limit_string, key_func=None):
    """Rate-limit decorator. Place BELOW @route so the wrapped view is what
    Flask registers — a previous bug stacked it on top, silently no-op'ing
    the limit. See app/routes/auth.py::conditional_rate_limit for the same
    contract.
    """

    def decorator(func):
        if os.getenv("TESTING"):
            return func
        kwargs = {"key_func": key_func} if key_func else {}
        return limiter.limit(limit_string, **kwargs)(func)

    return decorator


def _2fa_verify_key():
    """Rate-limit key: bind to the pending 2FA user_id stored in the session.

    A per-IP cap is bypassable by an attacker who already has a password
    and rotates IPs to brute-force the 6-digit TOTP. Per-user keying caps
    the absolute number of attempts on a single account regardless of IP.
    """
    from flask import session as _sess

    uid = _sess.get("pending_2fa_user_id")
    return f"2fa:{uid}" if uid else "2fa:anon"


def _2fa_disable_key():
    """Bug H1 (audit round 2) — `_2fa_verify_key` lit `pending_2fa_user_id`
    qui n'est PAS set quand le user est déjà authentifié (cas /2fa/disable).
    Tous les calls partageaient donc le bucket `"2fa:anon"` → un seul
    attaquant pouvait griller les 5/h pour TOUTE la plateforme. Pour
    /2fa/disable, l'user est par construction authentifié, on keye sur
    current_user.id directement.
    """
    if current_user.is_authenticated:
        return f"disable2fa:{current_user.id}"
    return "disable2fa:anon"


def _make_qr_b64(uri: str) -> str:
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _store_otp(code: str) -> None:
    session["otp_code"] = code
    session["otp_issued_at"] = time.time()
    session["otp_attempts"] = 0


def _verify_otp(submitted: str) -> bool:
    code = session.get("otp_code")
    issued_at = session.get("otp_issued_at", 0)
    return (
        code is not None
        and hmac.compare_digest(submitted, code)
        and time.time() - issued_at <= _OTP_TTL
    )


def _otp_expired() -> bool:
    return time.time() - session.get("otp_issued_at", 0) > _OTP_TTL


def _clear_otp():
    for k in ("otp_code", "otp_issued_at", "otp_attempts"):
        session.pop(k, None)


def _send_email_otp(destination: str, code: str) -> None:
    try:
        html = render_template("emails/email_otp_code.html", code=code)
        send_email_via_brevo_api(
            to=destination,
            subject="🔐 Votre code de vérification Myeline",
            html_content=html,
        )
    except Exception as exc:
        logger.error("Erreur envoi email 2FA : %s", exc)
    if current_app.config.get("FLASK_ENV") != "production":
        logger.warning("DEV — Email 2FA code sent to %s", destination)


def _resolve_acting_user():
    """Return (user, is_new_setup) for the current 2FA setup request.

    is_new_setup=True  → user arrived via login flow (pending_2fa_user_id),
                         must call _complete_login() after setup.
    is_new_setup=False → already authenticated user changing their 2FA method.
    Returns (None, False) if no valid user context is found.
    """
    if current_user.is_authenticated:
        return current_user._get_current_object(), False

    user_id = session.get("pending_2fa_user_id")
    if user_id:
        from app.models.user import User as _User

        user = db.session.get(_User, user_id)
        if user:
            return user, True
        session.pop("pending_2fa_user_id", None)

    return None, False


# ── Setup (choose method + configure) ────────────────────────────────────────


@user_2fa_bp.route("/2fa/setup", methods=["GET", "POST"])
def setup_2fa():
    # No @login_required: new users arrive here via pending_2fa_user_id before
    # their first login_user() call. Already-authenticated users can also reach
    # this page to change their 2FA method.
    acting_user, new_setup = _resolve_acting_user()
    if acting_user is None:
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        action = request.form.get("action")

        # ── TOTP ──────────────────────────────────────────────────────────────
        if action == "generate_totp":
            secret = pyotp.random_base32()
            session["pending_totp_secret"] = secret
            uri = pyotp.totp.TOTP(secret).provisioning_uri(
                acting_user.email, issuer_name=_ISSUER
            )
            qr_b64 = _make_qr_b64(uri)
            return render_template(
                "user/2fa_setup.html",
                step="confirm_totp",
                uri=uri,
                secret=secret,
                qr_b64=qr_b64,
            )

        if action == "confirm_totp":
            secret = session.get("pending_totp_secret")
            if not secret:
                flash("Session expirée. Recommencez.", "danger")
                return redirect(url_for("user_2fa.setup_2fa"))
            code = request.form.get("totp_code", "").strip()
            if pyotp.TOTP(secret).verify(code, valid_window=1):
                acting_user.totp_secret = secret
                acting_user.twofa_method = "totp"
                session.pop("pending_totp_secret", None)
                db.session.commit()
                logger.info("TOTP activé user_id=%s", acting_user.id)
                flash("Authentification par application activée.", "success")
                if new_setup:
                    return _complete_login(acting_user)
                _after_setup()
                return redirect(url_for("user.account_settings"))
            flash("Code invalide. Vérifiez l'heure de votre appareil.", "danger")
            uri = pyotp.totp.TOTP(secret).provisioning_uri(
                acting_user.email, issuer_name=_ISSUER
            )
            qr_b64 = _make_qr_b64(uri)
            return render_template(
                "user/2fa_setup.html",
                step="confirm_totp",
                uri=uri,
                secret=secret,
                qr_b64=qr_b64,
            )

        # ── Email OTP ─────────────────────────────────────────────────────────
        if action == "enable_email":
            code = "{:06d}".format(secrets.randbelow(1_000_000))
            _store_otp(code)
            _send_email_otp(acting_user.email, code)
            return render_template("user/2fa_setup.html", step="confirm_email")

        if action == "confirm_email":
            submitted = request.form.get("email_code", "").strip()
            if _otp_expired():
                flash("Code expiré. Recommencez.", "warning")
                return render_template("user/2fa_setup.html", step="choose")
            if not _verify_otp(submitted):
                flash("Code incorrect.", "danger")
                return render_template("user/2fa_setup.html", step="confirm_email")
            acting_user.twofa_method = "email"
            _clear_otp()
            db.session.commit()
            logger.info("Email 2FA activé user_id=%s", acting_user.id)
            flash("Authentification par email activée.", "success")
            if new_setup:
                return _complete_login(acting_user)
            _after_setup()
            return redirect(url_for("user.account_settings"))

    if acting_user.has_2fa and not new_setup:
        return render_template("user/2fa_setup.html", step="enabled")
    return render_template("user/2fa_setup.html", step="choose")


@user_2fa_bp.route("/2fa/disable", methods=["POST"])
@_conditional_rate_limit("5 per hour", key_func=_2fa_disable_key)
@login_required
def disable_2fa():
    # Rate-limited per user_id: an attacker who hijacks a session shouldn't be
    # able to brute-force the password gate by spamming this endpoint.
    if not current_user.check_password(request.form.get("password", "")):
        flash("Mot de passe incorrect.", "danger")
        return redirect(url_for("user_2fa.setup_2fa"))
    current_user.totp_secret = None
    current_user.twofa_method = None
    db.session.commit()
    logger.info("2FA désactivée user_id=%s", current_user.id)
    flash(
        "2FA désactivée. Vous devez en reconfigurer une pour accéder à votre compte.",
        "warning",
    )
    return redirect(url_for("user_2fa.setup_2fa"))


# ── Login-time 2FA verification ───────────────────────────────────────────────


@user_2fa_bp.route("/2fa/verify", methods=["GET", "POST"])
@_conditional_rate_limit("5 per minute", key_func=_2fa_verify_key)
def verify_2fa():
    user_id = session.get("pending_2fa_user_id")
    if not user_id:
        return redirect(url_for("auth.login"))

    from app.models.user import User

    user = db.session.get(User, user_id)
    if not user or not user.has_2fa:
        session.pop("pending_2fa_user_id", None)
        return redirect(url_for("auth.login"))

    method = user.twofa_method

    if method == "totp":
        error = None
        if request.method == "POST":
            code = request.form.get("totp_code", "").strip()
            if pyotp.TOTP(user.totp_secret).verify(code, valid_window=1):
                return _complete_login(user)
            error = "Code invalide. Réessayez."
        return render_template("log/2fa_verify.html", method="totp", error=error)

    # Email OTP — issue a code on the first GET, NOT on every reload.
    # Reloading the verify page used to mint a new code each time, which
    # both spammed Brevo and confused users (the previously delivered
    # code stopped working without warning). Only re-issue when the
    # session has no code or the previous one has expired.
    if request.method == "GET":
        if not session.get("otp_code") or _otp_expired():
            # Bug M6 (audit round 2) — rate limit séparé sur la
            # génération + envoi d'email OTP : éviter qu'un attaquant
            # contrôlant la session pre-login spamme la victime de
            # codes (déjà capé via le 5/min global sur la route, mais
            # 5 mails/min reste suffisant pour du harcèlement). Cap
            # supplémentaire à 1 envoi/60s par user via la session.
            import time as _t

            last_sent = session.get("otp_last_sent_at", 0)
            if _t.time() - last_sent >= 60:
                code = "{:06d}".format(secrets.randbelow(1_000_000))
                _store_otp(code)
                _send_email_otp(user.email, code)
                session["otp_last_sent_at"] = _t.time()
            else:
                logger.info("OTP email re-issue rate-limited user_id=%s", user.id)
        hint = user.email.split("@")[0][:3] + "***"
        return render_template("log/2fa_verify.html", method=method, hint=hint)

    submitted = request.form.get("otp_code", "").strip()
    if _otp_expired():
        flash("Code expiré. Reconnectez-vous.", "warning")
        session.pop("pending_2fa_user_id", None)
        return redirect(url_for("auth.login"))
    if not _verify_otp(submitted):
        # Invalidate the emailed code after repeated failures so a single OTP
        # can't be brute-forced across its full 10-min lifetime (L-6). Aligns
        # with the admin OTP, which is consumed before comparison.
        attempts = session.get("otp_attempts", 0) + 1
        session["otp_attempts"] = attempts
        if attempts >= 3:
            _clear_otp()
            session.pop("pending_2fa_user_id", None)
            flash(
                "Trop de codes incorrects. Reconnectez-vous pour en recevoir un nouveau.",
                "danger",
            )
            return redirect(url_for("auth.login"))
        hint = user.email.split("@")[0][:3] + "***"
        flash("Code incorrect.", "danger")
        return render_template("log/2fa_verify.html", method=method, hint=hint)

    _clear_otp()
    return _complete_login(user)


def _complete_login(user):
    from flask_login import login_user

    from app.utils.time_utils import utcnow

    session.pop("pending_2fa_user_id", None)
    _clear_otp()
    # Defence against session fixation pre-login: regenerate the session
    # identifier by clearing all keys before login_user binds the
    # authenticated state. An attacker who fixed the cookie pre-login
    # will hold a session that no longer maps to the new authenticated
    # state. (Audit H-7.)
    session.clear()
    login_user(user)
    user.last_login = utcnow()
    db.session.commit()
    logger.info("Connexion 2FA (%s) réussie user_id=%s", user.twofa_method, user.id)
    flash("Connexion réussie", "success")
    if user.is_admin:
        return redirect(url_for("admin.dashboard"))
    return redirect(url_for("user.dashboard"))


def _after_setup():
    session.pop("2fa_setup_required", None)
