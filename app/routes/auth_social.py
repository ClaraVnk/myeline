"""
OAuth social login: Google, Microsoft, Apple.

Flow (all providers):
  GET  /auth/social/<provider>           → redirect to provider
  GET  /auth/social/<provider>/callback  → handle token + user info
  POST /auth/social/apple/callback       → Apple uses form_post response mode

On callback:
  1. Fetch user sub + email from id_token / userinfo endpoint
  2. Look up OAuthAccount(provider, provider_user_id)
     a. Found           → login that user
     b. Not found, email matches existing user → link + login
     c. Not found, new  → create User (confirmed, no password) + OAuthAccount + login
"""
import secrets
import time

from flask import Blueprint, abort, current_app, flash, redirect, request, session, url_for
from flask_login import login_user
from werkzeug.security import generate_password_hash

from app.ext import db, oauth
from app.models.oauth_account import OAuthAccount
from app.models.user import User
from app.utils.logger import get_logger
from app.utils.time_utils import utcnow


logger = get_logger("auth_social", subfolder="routes")

auth_social_bp = Blueprint("auth_social", __name__)

_PROVIDERS = ("google", "microsoft", "apple")


@auth_social_bp.before_request
def _gate_social_login_in_on_prem():
    """Disable /auth/social/* on every on-prem deployment.

    Sovereign air-gap can't reach Google/Microsoft endpoints anyway,
    and in hybrid we deliberately route enterprise users through OIDC
    SSO (per-org config at /org/<slug>/oidc). Returning 404 hides the
    feature from probing tools — there's no point telling a hybrid
    operator about a route they shouldn't use.
    """
    from app.utils.deployment_mode import is_on_prem
    if is_on_prem():
        from flask import abort
        abort(404)


# ── Apple client-secret (short-lived JWT, generated on demand) ────────────────

def _apple_client_secret(ttl_seconds: int = 300) -> str:
    """Build a short-lived ES256 JWT for the Apple token endpoint.

    Bug H5 (audit round 2) — l'ancien code mintait un JWT exp=180j au
    boot, vivant en mémoire process. Time bomb après 6 mois sans
    redéploiement, et large fenêtre de fuite via heap dump. Désormais
    régénéré juste avant chaque token exchange Apple (cf. social_callback)
    avec exp=5min par défaut. Le boot conserve un secret plus long
    (30j) en filet de sécurité au cas où un chemin manquerait l'appel
    de refresh.
    """
    from authlib.jose import jwt as jose_jwt

    cfg = current_app.config
    team_id = cfg.get("APPLE_LOGIN_TEAM_ID", "")
    client_id = cfg.get("APPLE_LOGIN_CLIENT_ID", "")
    key_id = cfg.get("APPLE_LOGIN_KEY_ID", "")
    private_key = cfg.get("APPLE_LOGIN_PRIVATE_KEY", "")

    if not all([team_id, client_id, key_id, private_key]):
        return ""

    now = int(time.time())
    header = {"alg": "ES256", "kid": key_id}
    payload = {
        "iss": team_id,
        "iat": now,
        "exp": now + ttl_seconds,
        "aud": "https://appleid.apple.com",
        "sub": client_id,
    }
    return jose_jwt.encode(header, payload, private_key).decode("utf-8")


# ── OAuth entry point ─────────────────────────────────────────────────────────

@auth_social_bp.route("/auth/social/<provider>")
def social_login(provider: str):
    if provider not in _PROVIDERS:
        abort(404)

    cfg = current_app.config
    client = _get_or_none(provider)
    if client is None:
        flash("Ce fournisseur n'est pas configuré.", "warning")
        return redirect(url_for("auth.login"))

    base = current_app.config.get("OAUTH_REDIRECT_BASE_URL", "").rstrip("/")
    redirect_uri = (base + url_for("auth_social.social_callback", provider=provider)) if base else url_for("auth_social.social_callback", provider=provider, _external=True)

    # Apple needs a nonce for OIDC
    if provider == "apple":
        nonce = secrets.token_urlsafe(16)
        session["apple_nonce"] = nonce
        return client.authorize_redirect(
            redirect_uri,
            response_mode="form_post",
            nonce=nonce,
        )

    return client.authorize_redirect(redirect_uri)


# ── Callback (GET for Google/Microsoft, POST for Apple) ───────────────────────

@auth_social_bp.route("/auth/social/<provider>/callback", methods=["GET", "POST"])
def social_callback(provider: str):
    if provider not in _PROVIDERS:
        abort(404)

    client = _get_or_none(provider)
    if client is None:
        abort(404)

    try:
        # Microsoft multi-tenant: the 'iss' in the id_token contains the real
        # tenant ID (e.g. login.microsoftonline.com/{tid}/v2.0) which never
        # matches the /common/ metadata issuer. Skip that claim check.
        token_kwargs = {}
        if provider == "microsoft":
            token_kwargs["claims_options"] = {"iss": {"essential": False}}
        # Bug H5 (audit round 2) — l'ancien code utilisait le client_secret
        # JWT Apple calculé UNE FOIS au boot avec exp=180j. Au-delà, Apple
        # rejette tout token exchange jusqu'au redémarrage worker. On
        # régénère un JWT court (5 min) juste avant chaque token exchange.
        if provider == "apple":
            client.client_secret = _apple_client_secret()
        token = client.authorize_access_token(**token_kwargs)
    except Exception as exc:
        logger.warning("OAuth callback error provider=%s : %s", provider, exc)
        flash("Authentification échouée. Réessayez.", "danger")
        return redirect(url_for("auth.login"))

    user_info = _extract_user_info(provider, token)
    if not user_info.get("sub"):
        flash("Impossible de récupérer les informations du compte.", "danger")
        return redirect(url_for("auth.login"))

    return _login_or_create(provider, user_info)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_or_none(provider: str):
    """Return the registered Authlib client for *provider*, or None if unconfigured."""
    cfg = current_app.config
    if provider == "google" and cfg.get("GOOGLE_LOGIN_CLIENT_ID"):
        return oauth.google
    if provider == "microsoft" and cfg.get("MICROSOFT_LOGIN_CLIENT_ID"):
        return oauth.microsoft
    if provider == "apple" and cfg.get("APPLE_LOGIN_CLIENT_ID"):
        return oauth.apple
    return None


def _extract_user_info(provider: str, token: dict) -> dict:
    """Return {'sub','email','name','email_verified'} from the token /
    userinfo. `email_verified` is the IdP claim — it must be True before
    we trust the email for account linking. See audit H-4.
    """
    if provider in ("google", "microsoft"):
        try:
            id_token = token.get("userinfo") or {}
        except Exception:
            id_token = {}
        sub = id_token.get("sub") or ""
        email = id_token.get("email") or ""
        name = id_token.get("name") or ""
        # Google: claim is in the id_token. Microsoft: not always present
        # (depends on tenant config) — when missing we treat the email
        # as unverified to be safe.
        email_verified = bool(id_token.get("email_verified"))
        return {"sub": sub, "email": email, "name": name, "email_verified": email_verified}

    if provider == "apple":
        id_token_raw = token.get("id_token")
        if not id_token_raw:
            return {}
        from authlib.jose import JsonWebToken
        jwks_uri = "https://appleid.apple.com/auth/keys"
        try:
            # Bug H3 — validate() par défaut ne vérifie que exp/nbf/iat.
            # iss et aud ne sont PAS vérifiés sans claims_options →
            # un id_token Apple émis pour une autre app passe la
            # signature (JWKS publique) et est accepté ici. Account
            # takeover trivial.
            #
            # Fix : forcer claims_options pour iss + aud + (bug M4) nonce.
            apple_client_id = (
                current_app.config.get("APPLE_LOGIN_CLIENT_ID", "") or ""
            )
            expected_nonce = session.pop("apple_nonce", None)
            claims_options = {
                "iss": {"essential": True, "value": "https://appleid.apple.com"},
                "aud": {"essential": True, "value": apple_client_id},
            }
            if expected_nonce:
                claims_options["nonce"] = {"essential": True, "value": expected_nonce}

            # Apple signs its id_tokens with RS256 today; the explicit
            # allow-list avoids the "alg":"none" / algorithm-confusion class
            # of attacks that an unrestricted JsonWebToken() would permit.
            from app.utils.safe_http import safe_get
            jwks = safe_get(jwks_uri, timeout=5).json()
            claims = JsonWebToken(["RS256"]).decode(
                id_token_raw, jwks, claims_options=claims_options,
            )
            claims.validate()
        except Exception as exc:
            logger.warning("Apple id_token decode/validate error: %s", exc)
            return {}

        # Apple only returns email + name on first authorization
        sub = claims.get("sub", "")
        email = claims.get("email", "")
        # Apple returns "email_verified" as either bool or "true"/"false" string.
        ev_raw = claims.get("email_verified")
        if isinstance(ev_raw, bool):
            email_verified = ev_raw
        elif isinstance(ev_raw, str):
            email_verified = ev_raw.lower() == "true"
        else:
            email_verified = False
        # name may be in the POST body on first login
        user_data = {}
        if request.method == "POST":
            import json
            raw = request.form.get("user", "")
            if raw:
                try:
                    user_data = json.loads(raw)
                except ValueError:
                    pass
        name_obj = user_data.get("name", {})
        name = f"{name_obj.get('firstName', '')} {name_obj.get('lastName', '')}".strip()
        return {"sub": sub, "email": email, "name": name, "email_verified": email_verified}

    return {}


def _login_or_create(provider: str, info: dict):
    sub = info["sub"]
    email = (info.get("email") or "").lower().strip()
    name = info.get("name", "")
    email_verified = bool(info.get("email_verified"))

    # 1 — Known OAuth identity (already linked from a prior verified login)
    account = OAuthAccount.query.filter_by(
        provider=provider, provider_user_id=sub
    ).first()
    if account:
        # Bug H2 (audit round 2) — l'ancien code ignorait la valeur de
        # retour de _do_login (qui peut être une redirect vers /2fa/verify
        # pour les users 2FA). Conséquence : 2FA pending mais redirect
        # vers dashboard, login_required rejette → boucle UX. Et
        # fragilité critique si dashboard n'avait pas login_required.
        return _do_login(account.user)

    # 2 — Existing user with matching email
    existing_user = User.query.filter_by(email=email).first() if email else None
    if existing_user:
        # SECURITY (audit H-4): refuse to auto-link an existing account when
        # the IdP did not assert that the email is verified. Otherwise an
        # attacker who registers a self-hosted Microsoft tenant can mint a
        # token with any email claim and take over the matching Myeline
        # account on first click.
        if not email_verified:
            logger.warning(
                "OAuth refused auto-link on unverified email user_id=%s provider=%s",
                existing_user.id, provider,
            )
            flash(
                "Votre fournisseur d'identité n'a pas confirmé que vous possédez "
                "cette adresse email. Connectez-vous avec votre mot de passe Myeline "
                "pour lier ce compte vous-même.",
                "danger",
            )
            return redirect(url_for("auth.login"))

        # SECURITY (audit C-3 / H-4): never auto-link a social account onto
        # an admin row. Admins authenticate through the dedicated admin
        # login flow with its own 2FA and rate limit.
        if existing_user.is_admin:
            logger.warning(
                "OAuth refused auto-link on admin account user_id=%s provider=%s",
                existing_user.id, provider,
            )
            flash(
                "Les comptes administrateur doivent se connecter via /admin/login.",
                "danger",
            )
            return redirect(url_for("auth.login"))

        account = OAuthAccount(
            user_id=existing_user.id,
            provider=provider,
            provider_user_id=sub,
            email=email or None,
        )
        db.session.add(account)

        # If the user was pre-created via org invite and not yet confirmed,
        # a verified social login counts as email verification — confirm
        # and activate. Email_verified was already enforced above.
        if not existing_user.is_confirmed:
            existing_user.is_confirmed = True
            logger.info("OAuth confirmed pre-invite user_id=%s provider=%s", existing_user.id, provider)

        db.session.commit()
        logger.info("OAuth account lié user_id=%s provider=%s", existing_user.id, provider)
        # Bug H2 — cf. ci-dessus, retour de _do_login.
        return _do_login(existing_user)

    # 3 — New user
    if not email:
        flash(
            "Votre compte Apple n'a pas partagé d'email. "
            "Autorisez le partage d'email dans vos réglages Apple.",
            "danger",
        )
        return redirect(url_for("auth.login"))

    firstname, *rest = (name.split(" ", 1) if name else ["", ""])
    lastname = rest[0] if rest else ""

    new_user = User(
        email=email,
        firstname=firstname or None,
        lastname=lastname or None,
        # Unusable password — social users authenticate via OAuth only
        password_hash=generate_password_hash(secrets.token_urlsafe(32)),
        is_confirmed=True,
        is_active=True,
        created_at=utcnow(),
    )

    db.session.add(new_user)
    db.session.flush()

    account = OAuthAccount(
        user_id=new_user.id,
        provider=provider,
        provider_user_id=sub,
        email=email,
    )
    db.session.add(account)
    db.session.commit()
    logger.info("Nouvel utilisateur OAuth user_id=%s provider=%s", new_user.id, provider)

    # Session fixation defence (audit H-7).
    from flask import session as _sess
    _sess.clear()
    login_user(new_user)
    new_user.last_login = utcnow()
    db.session.commit()

    flash("Bienvenue sur Myeline ! Votre compte est prêt.", "success")
    return redirect(url_for("user.dashboard"))


def _do_login(user: User):
    if not user.is_active:
        flash("Votre compte est désactivé.", "danger")
        return redirect(url_for("auth.login"))
    # Session fixation defence (audit H-7).
    from flask import session as _sess
    _sess.clear()

    # Bug H2 — l'ancien comportement faisait login_user(user) direct
    # sans vérifier user.has_2fa. Les users qui avaient configuré une
    # 2FA locale (TOTP/email/SMS) étaient pleinement authentifiés via
    # OAuth sans présenter leur second facteur — compromission du
    # compte Google/Microsoft = accès direct à Myeline bypassant la
    # 2FA "obligatoire". Désormais on respecte le même flow que
    # /auth/login : si has_2fa, route vers pending_2fa_user_id +
    # verify_2fa (même UX que login password). Les comptes
    # OAuth-review explicitement listés dans BYPASS_2FA_EMAILS
    # restent exemptés (cf. is_2fa_bypassed).
    from app.utils.twofa_bypass import is_2fa_bypassed
    if user.has_2fa and not is_2fa_bypassed(user):
        _sess["pending_2fa_user_id"] = user.id
        # `oauth_completed` est un flag pour que verify_2fa sache
        # qu'on vient d'un OAuth callback (pas d'un /auth/login
        # form) — pour skipper la re-vérification password.
        _sess["pending_2fa_via_oauth"] = True
        logger.info(
            "OAuth login pending 2FA for user_id=%s (will redirect to verify_2fa)",
            user.id,
        )
        return redirect(url_for("user_2fa.verify_2fa"))

    login_user(user)
    user.last_login = utcnow()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    logger.info("Connexion OAuth réussie user_id=%s", user.id)
    flash("Connexion réussie.", "success")
    # Bug H2 — l'ancien `_do_login` n'avait pas de return final ; les
    # callers faisaient `_do_login(user); return redirect(dashboard)`.
    # Désormais c'est `return _do_login(user)` qui doit toujours
    # retourner une Response.
    return redirect(url_for("user.dashboard"))
