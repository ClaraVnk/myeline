import hmac
import os

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, logout_user
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from app.ext import db, limiter
from app.forms.auth_forms import ForgotPasswordForm, ResetPasswordForm
from app.forms.user_forms import LoginForm, RegisterForm
from app.models import User
from app.services.user_services import is_strong_password
from app.utils.logger import get_logger
from app.utils.mailer import send_email_via_brevo_api
from app.utils.recaptcha import verify_recaptcha
from app.utils.time_utils import utcnow
from app.utils.tokens import confirm_token, generate_confirmation_token


# Create a conditional decorator for rate limiting
def conditional_rate_limit(limit_string, key_func=None):
    """Apply rate limiting conditionally based on environment.

    Decorator ORDER MATTERS: place this **below** `@auth_bp.route(...)` so
    Flask registers the rate-limited wrapper, not the bare view. A previous
    audit (2026-05-06) caught all auth endpoints stacking this above the
    @route, which silently no-op'd the limit. Pattern is now consistently:

        @auth_bp.route("/login", methods=["GET", "POST"])
        @conditional_rate_limit("5 per minute")
        def login():
            ...

    `key_func` lets callers key on something other than the remote IP — e.g.
    `lambda: request.form.get("email", "")` to throttle per-email and defeat
    distributed password spraying.
    """

    def decorator(func):
        if os.getenv("TESTING"):
            return func
        kwargs = {"key_func": key_func} if key_func else {}
        return limiter.limit(limit_string, **kwargs)(func)

    return decorator


def _login_email_key():
    """Rate-limit key: lower-cased submitted email, falling back to remote IP.

    Defeats distributed credential-stuffing where one IP tries one password
    against many emails (per-IP limit doesn't catch it; per-email does).
    """
    from flask import request as _req

    email = (_req.form.get("email") or "").strip().lower()
    if email:
        return f"email:{email}"
    return limiter._key_func() if hasattr(limiter, "_key_func") else "anon"


def _password_hash_fingerprint(password_hash: str) -> str:
    """SHA-256 hex of the user's password_hash, used as a single-use
    fingerprint inside password-reset tokens. When the user successfully
    resets, password_hash changes (random salt regenerates), this
    fingerprint changes too, and any in-flight token fails validation.

    Full SHA-256 hex closes the audit M-5 collision risk vs the previous
    `password_hash[-12:]` (72 bits) approach.
    """
    # Bug L1 (audit round 2) — l'ancien `usedforsecurity=False` était
    # trompeur : ce hash EST cryptographique (anti-replay du token de
    # reset). Le flag signale "OK même en FIPS mode" pour MD5/SHA1, sans
    # effet sur SHA-256, mais induit un faux signal de "non sécurité" en
    # revue. Retiré.
    import hashlib as _h

    return _h.sha256(password_hash.encode("utf-8")).hexdigest()


auth_bp = Blueprint("auth", __name__)

logger = get_logger("auth", subfolder="routes")


@auth_bp.route("/login", methods=["GET", "POST"])
# Bug M1 (audit round 2) — l'ancien cap per-email seul (5/min) laissait
# un attaquant DoS un compte victime en spammant `/login` avec
# `email=victim@x.com` + password vide. Limite per-email rehaussée à
# 50/h (réaliste pour un humain), et limite per-IP plus stricte ajoutée
# (10/min) qui empêche un attaquant unique de tourner sur N emails.
@conditional_rate_limit("10 per minute")  # per-IP via le default key_func
@conditional_rate_limit("50 per hour", key_func=_login_email_key)
def login():
    form = LoginForm()
    if form.validate_on_submit():
        token = request.form.get("g-recaptcha-response", "")
        if not verify_recaptcha(token, "login"):
            logger.warning("Échec vérification reCAPTCHA lors de la connexion.")
            flash(
                "Vérification de sécurité échouée. Attendez quelques secondes et réessayez. Si le problème persiste, désactivez votre bloqueur de publicités.",
                "danger",
            )
            return render_template("log/login.html", form=form)
        logger.debug("Tentative de connexion")
        user = User.query.filter_by(email=form.email.data).first()

        if user is None:
            # Equalise response time so a non-existent account isn't
            # distinguishable from a wrong password by timing — the argon2
            # verify below only runs when the user exists (A4 / L-3).
            from app.models.user import dummy_password_check

            dummy_password_check(form.password.data)

        if user and user.check_password(form.password.data):
            if not user.is_confirmed and not user.is_admin:
                logger.warning(
                    "Connexion refusée : email non confirmé user_id=%s", user.id
                )
                # A4-M1: don't tell the caller the account exists but is
                # unconfirmed — that's free email-enumeration. Generic
                # message, same template as a wrong-password rejection.
                # Users who forgot to confirm can hit /resend-confirmation
                # directly from the login page.
                flash("Identifiants invalides", "danger")
                return render_template("log/login.html", form=form)

            if user.is_admin:
                # Admins always go through the dedicated admin login (email OTP).
                session["admin_pwd_verified"] = user.id
                logger.info(
                    "Admin redirigé vers admin_login pour OTP user_id=%s", user.id
                )
                return redirect(url_for("admin.admin_login"))

            # OAuth-review test accounts (BYPASS_2FA_EMAILS env) skip
            # the entire 2FA flow — Google / Microsoft / Dropbox app-
            # review automation cannot answer TOTP challenges. Real users
            # never appear in that list (see app/utils/twofa_bypass.py).
            from app.utils.twofa_bypass import is_2fa_bypassed

            if is_2fa_bypassed(user):
                from flask import session as _sess
                from flask_login import login_user as _login

                _sess.clear()  # session-fixation defence
                _login(user)
                user.last_login = utcnow()
                db.session.commit()
                logger.info("OAuth-review login bypassing 2FA user_id=%s", user.id)
                flash("Connexion réussie", "success")
                return redirect(url_for("user.dashboard"))

            # 2FA is mandatory — always redirect to verify/setup
            session["pending_2fa_user_id"] = user.id
            if user.has_2fa:
                logger.info("2FA (%s) requise user_id=%s", user.twofa_method, user.id)
                return redirect(url_for("user_2fa.verify_2fa"))
            else:
                logger.info(
                    "2FA non configurée — setup obligatoire user_id=%s", user.id
                )
                session["2fa_setup_required"] = True
                # Do NOT call login_user() here — the user is not authenticated
                # until they complete 2FA setup. setup_2fa resolves the user via
                # pending_2fa_user_id and calls _complete_login() after setup.
                return redirect(url_for("user_2fa.setup_2fa"))
        else:
            logger.warning("Échec de connexion")
            flash("Identifiants invalides", "danger")

    return render_template("log/login.html", form=form)


@auth_bp.route("/register", methods=["GET", "POST"])
@conditional_rate_limit("3 per minute")
def register():
    form = RegisterForm()
    # Pivot Free/Pro/Enterprise : le POST de ce formulaire crée TOUJOURS un
    # compte Free. Le parcours Pro NE passe PAS par ce POST — il est piloté
    # en JS depuis le template (prepare_checkout → create_checkout_session →
    # Stripe redirect), donc le serveur ne voit jamais un submit "Pro" ici.
    # On lit malgré tout `plan` (depuis l'URL ?plan=pro ou un re-render) pour
    # pré-sélectionner la bonne carte côté template, et on ne propose Pro que
    # si Stripe est disponible (classic) — sur on-prem, Free uniquement.
    from app.utils.deployment_mode import has_stripe

    requested_plan = (request.values.get("plan") or "").strip().lower()
    plan = "pro" if (requested_plan == "pro" and has_stripe()) else "gratuit"
    billing = "monthly"
    # Bug L1 — l'ancien check verify_recaptcha tournait à chaque GET (qui
    # n'a pas de g-recaptcha-response) → appel Google à chaque chargement
    # de page + faux négatif systématique + log spammé. Ne vérifier qu'en
    # POST (le check `validate_on_submit` ci-dessous garantit POST aussi).
    if request.method == "POST":
        token = request.form.get("g-recaptcha-response", "")
        if not verify_recaptcha(token, "register"):
            logger.warning("Échec vérification reCAPTCHA lors de l'inscription.")
            flash(
                "Vérification de sécurité échouée. Attendez quelques secondes et réessayez. Si le problème persiste, désactivez votre bloqueur de publicités.",
                "danger",
            )
            return render_template("log/register.html", form=form, plan=plan)
    if form.validate_on_submit():
        logger.debug("Tentative d'inscription")
        if not is_strong_password(form.password.data):
            logger.warning("Mot de passe faible à l'inscription")
            flash(
                (
                    "Le mot de passe doit contenir au moins 8 caractères, dont une majuscule, "
                    "une minuscule, un chiffre et un caractère spécial."
                ),
                "danger",
            )
            return render_template("log/register.html", form=form, plan=plan)

        user = User(
            firstname=form.firstname.data,
            lastname=form.lastname.data,
            email=form.email.data,
            password_hash=generate_password_hash(form.password.data),
            is_admin=False,
        )

        token = generate_confirmation_token(user.email)
        user.confirmation_token = token

        db.session.add(user)
        # Audit round 4 — A1-H2. Two concurrent POST /register requests with
        # the same email used to both pass the (implicit) "email available"
        # check and then race on the INSERT, where one succeeded and the
        # other raised IntegrityError on the unique constraint with a 500
        # page that left the user / session in an undefined state. We now
        # catch the IntegrityError explicitly, roll back, and surface a
        # friendly "compte déjà existant" flash so the user falls back to
        # the login flow.
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            logger.warning(
                "register_unique_violation email=%s plan=%s — likely concurrent submit",
                (form.email.data or "")[:3] + "***",
                plan,
            )
            flash(
                "Un compte existe déjà avec cet email. Connectez-vous ou utilisez "
                "« Mot de passe oublié » si nécessaire.",
                "warning",
            )
            return redirect(url_for("auth.login"))

        logger.info("Nouvel utilisateur inscrit user_id=%s (Free)", user.id)

        # Pivot Free/Pro/Enterprise : l'inscription crée TOUJOURS un compte
        # Free (gratuit) — abonnement_type par défaut sur le modèle. La
        # bascule en Pro se fait plus tard via /payment (pas au register).
        # On envoie simplement l'email de confirmation pour activer le compte.
        try:
            from app.utils.mailer import send_email_via_brevo_api

            confirm_url = url_for("auth.confirm_email", token=token, _external=True)
            send_email_via_brevo_api(
                to=user.email,
                subject="Confirmez votre inscription",
                html_content=render_template(
                    "emails/email_welcome.html", user=user, confirm_url=confirm_url
                ),
            )
        except Exception:
            logger.exception("Erreur envoi email de confirmation user_id=%s", user.id)
        # Page de confirmation dédiée (PRG) plutôt qu'un flash sur /login :
        # l'utilisateur voit clairement que le compte est créé ET qu'il doit
        # confirmer par email (en pensant au dossier spam). Le flag session
        # évite l'accès direct à la page de succès hors parcours d'inscription.
        session["just_registered"] = True
        return redirect(url_for("auth.register_success"))

    return render_template("log/register.html", form=form, plan=plan, billing=billing)


@auth_bp.route("/register/confirmation")
def register_success():
    """Confirmation page shown right after a successful registration.

    Gated on the ``just_registered`` session flag set by register() so the
    page isn't reachable (and indexable) out of the signup flow — a direct
    hit just bounces to /login.
    """
    if not session.pop("just_registered", False):
        return redirect(url_for("auth.login"))
    return render_template("log/register_success.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logger.info("Déconnexion user_id=%s", getattr(current_user, "id", None))
    logout_user()
    flash("Déconnexion réussie", "success")
    return redirect(url_for("home.welcome_page"))


@auth_bp.route("/first-password-reset", methods=["GET"])
@login_required
def first_password_reset():
    """Affiche le formulaire de reset de password forcé pour les users
    avec must_reset_password=True (admins tenant qui ont reçu un password
    initial par email en clair, bug 22).
    """
    if not getattr(current_user, "must_reset_password", False):
        return redirect(url_for("user.dashboard"))
    return render_template("auth/first_password_reset.html")


@auth_bp.route("/first-password-reset", methods=["POST"])
@login_required
def first_password_reset_post():
    from app.services.user_services import is_strong_password

    if not getattr(current_user, "must_reset_password", False):
        return redirect(url_for("user.dashboard"))
    new_password = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    if new_password != confirm:
        flash("Les deux mots de passe ne correspondent pas.", "danger")
        return redirect(url_for("auth.first_password_reset"))
    if not is_strong_password(new_password):
        flash(
            "Mot de passe trop faible — 8 caractères minimum, une majuscule, "
            "une minuscule, un chiffre et un caractère spécial.",
            "danger",
        )
        return redirect(url_for("auth.first_password_reset"))
    current_user.set_password(new_password)
    current_user.must_reset_password = False
    db.session.commit()
    flash(
        "Mot de passe mis à jour. Vous pouvez maintenant utiliser l'application.",
        "success",
    )
    return redirect(url_for("user.dashboard"))


@auth_bp.route("/confirm/<token>")
def confirm_email(token):
    email = confirm_token(token)
    if not email:
        logger.warning("Token de confirmation invalide ou expiré")
        flash(
            "⛔ Ce lien est invalide ou expiré. Vous pouvez demander un nouvel email de confirmation.",
            "danger",
        )
        return render_template("log/resend_confirmation_page.html")

    user = User.query.filter_by(email=email).first_or_404()
    if user.is_confirmed:
        # Bug L2 (audit round 2) — l'ancien message "déjà confirmé"
        # leak l'existence du compte (enumeration). Message neutre
        # identique au cas token invalide pour ne pas distinguer les
        # deux cas observables.
        logger.info("Compte déjà confirmé (token rejoué) user_id=%s", user.id)
        flash(
            "✅ Votre compte est actif. Connectez-vous avec vos identifiants.",
            "info",
        )
    else:
        user.is_confirmed = True
        user.confirmation_token = None
        db.session.commit()
        logger.info("Email confirmé user_id=%s", user.id)
        flash("Votre adresse email a été confirmée avec succès.", "success")

    return redirect(url_for("auth.login"))


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
# Bug M1 — combiner per-email (anti-spam de l'inbox de la victime) ET
# per-IP (anti-DoS ciblé sur un compte tiers). Cap per-email rehaussé
# à 10/h (plusieurs sessions navigateur / appareils légitimes).
@conditional_rate_limit("20 per hour")  # per-IP
@conditional_rate_limit("10 per hour", key_func=_login_email_key)
def forgot_password():
    form = ForgotPasswordForm()

    # Bug H3 (audit round 2) — l'ancien code tournait verify_recaptcha
    # sur tout chargement GET (token vide → False → flash danger + appel
    # API Google à chaque hit). Faux négatif systématique pour l'user
    # légitime + DoS soft sur le quota reCAPTCHA. On encadre dans la
    # branche POST, comme /register.
    if request.method == "POST":
        token = request.form.get("g-recaptcha-response", "")
        if not verify_recaptcha(token, "forgot_password"):
            logger.warning(
                "Échec vérification reCAPTCHA pour réinitialisation mot de passe."
            )
            flash(
                "Vérification de sécurité échouée. Attendez quelques secondes et réessayez. Si le problème persiste, désactivez votre bloqueur de publicités.",
                "danger",
            )
            return render_template("log/forgot_password.html", form=form)

    if form.validate_on_submit():
        logger.debug("Demande de réinitialisation de mot de passe")
        user = User.query.filter_by(email=form.email.data).first()
        if user:
            serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"])
            # `ph` is a fingerprint of the user's current password_hash —
            # changing the password (which is what reset_password does on
            # success) flips this value, invalidating any in-flight token.
            # Full SHA-256 hex (vs the previous 12 chars of base64) closes
            # the audit M-5 collision concern: password_hash includes a
            # random salt so two different hashes can't share the
            # fingerprint without breaking SHA-256 itself.
            ph_fp = _password_hash_fingerprint(user.password_hash)
            token_payload = {"email": user.email, "ph": ph_fp}
            token = serializer.dumps(token_payload, salt="password-reset-salt")
            reset_url = url_for("auth.reset_password", token=token, _external=True)
            html = render_template(
                "emails/email_reset_password.html", user=user, reset_url=reset_url
            )
            try:
                send_email_via_brevo_api(
                    to=user.email,
                    subject="Réinitialisation de votre mot de passe",
                    html_content=html,
                )
                logger.info("Email de réinitialisation envoyé user_id=%s", user.id)
            except Exception as e:
                logger.error("Erreur envoi email réinitialisation : %s", e)
                flash(
                    "Une erreur est survenue lors de l'envoi de l'email. Veuillez réessayer plus tard.",
                    "danger",
                )
                return render_template("log/forgot_password.html", form=form)
        flash(
            "Si cet email est associé à un compte, un lien de réinitialisation a été envoyé.",
            "info",
        )
        return redirect(url_for("auth.login"))

    return render_template("log/forgot_password.html", form=form)


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
@conditional_rate_limit("10 per minute")
def reset_password(token):
    serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"])
    try:
        data = serializer.loads(token, salt="password-reset-salt", max_age=3600)
    except Exception:
        logger.warning("Token de réinitialisation invalide ou expiré")
        flash("⛔ Ce lien est invalide ou expiré.", "danger")
        return redirect(url_for("auth.forgot_password"))

    email = data["email"] if isinstance(data, dict) else data
    ph_fingerprint = data.get("ph") if isinstance(data, dict) else None

    user = User.query.filter_by(email=email).first_or_404()

    # Bug M5 — l'ancien check `if ph_fingerprint is not None and not fp_ok`
    # skippait silencieusement l'invariant "token invalidé par reset"
    # pour les anciens tokens (format dict sans `ph` ou string brute).
    # Tous les nouveaux tokens incluent `ph` depuis audit M-5 →
    # n'importe quel token sans `ph` est nécessairement obsolète OU
    # forgé. On REFUSE catégoriquement. La fenêtre max_age=3600 limitait
    # l'impact mais ouvrait une faille pendant 1h après un reset.
    if not isinstance(data, dict) or ph_fingerprint is None:
        logger.warning(
            "Token de réinitialisation legacy (sans ph) rejeté pour email=%s",
            email,
        )
        flash("⛔ Ce lien est obsolète. Demandez un nouveau lien.", "danger")
        return redirect(url_for("auth.forgot_password"))

    # Constant-time compare — protects against the (negligible but
    # cheap to close) timing channel that audit M-5 mentioned.
    expected_fp = _password_hash_fingerprint(user.password_hash)
    fp_ok = len(ph_fingerprint) == len(expected_fp) and hmac.compare_digest(
        str(ph_fingerprint), expected_fp
    )
    if not fp_ok:
        logger.warning("Token de réinitialisation déjà utilisé user_id=%s", user.id)
        flash("⛔ Ce lien a déjà été utilisé. Demandez un nouveau lien.", "danger")
        return redirect(url_for("auth.forgot_password"))

    form = ResetPasswordForm()

    if form.validate_on_submit():
        recaptcha_token = request.form.get("recaptcha_token", "")
        if not verify_recaptcha(recaptcha_token, "reset_password"):
            logger.warning("Échec vérification reCAPTCHA pour réinitialisation.")
            flash(
                "Vérification de sécurité échouée. Attendez quelques secondes et réessayez. Si le problème persiste, désactivez votre bloqueur de publicités.",
                "danger",
            )
            return render_template("log/reset_password.html", form=form)

        user.set_password(form.password.data)
        db.session.commit()
        logger.info("Mot de passe réinitialisé user_id=%s", user.id)
        flash("Votre mot de passe a été réinitialisé avec succès.", "success")
        return redirect(url_for("auth.login"))

    return render_template("log/reset_password.html", form=form)


@auth_bp.route("/resend-confirmation", methods=["GET", "POST"])
# 3 per hour per email — same anti-spam guarantee as /forgot-password. The
# confirmation token has its own expiry; the rate limit caps how often we
# send a fresh one, not the validity window of an already-issued token.
@conditional_rate_limit("3 per hour", key_func=_login_email_key)
def resend_confirmation():
    if request.method == "POST":
        token = request.form.get("recaptcha_token", "")
        if not verify_recaptcha(token, "resend_confirmation"):
            logger.warning("Échec vérification reCAPTCHA pour renvoi confirmation.")
            flash(
                "Vérification de sécurité échouée. Attendez quelques secondes et réessayez. Si le problème persiste, désactivez votre bloqueur de publicités.",
                "danger",
            )
            return render_template(
                "log/resend_confirmation_page.html", confirmation_sent=False
            )
        email = request.form.get("email")
        user = User.query.filter_by(email=email).first()
        if user and not user.is_confirmed:
            token = generate_confirmation_token(user.email)
            user.confirmation_token = token
            db.session.commit()
            confirm_url = url_for(
                "auth.confirm_email",
                token=token,
                _external=True,
            )
            send_email_via_brevo_api(
                to=user.email,
                subject="Confirmation de votre compte MGnews",
                html_content=render_template(
                    "emails/email_welcome.html",
                    user=user,
                    confirm_url=confirm_url,
                ),
            )
            logger.info("Email de confirmation renvoyé user_id=%s", user.id)
            return render_template(
                "log/resend_confirmation_page.html",
                confirmation_sent=True,
            )
        else:
            logger.warning("Renvoi confirmation : déjà confirmé ou introuvable")
            flash("Adresse non trouvée ou déjà confirmée.", "warning")
            return redirect(url_for("auth.resend_confirmation"))

    return render_template("log/resend_confirmation_page.html", confirmation_sent=False)
