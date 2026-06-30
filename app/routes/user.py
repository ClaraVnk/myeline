from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import joinedload

from app.ext import db
from app.forms.user_forms import ContactForm
from app.models import Newsletter
from app.services import rag_service
from app.services.user_services import is_strong_password
from app.utils.logger import get_logger
from app.utils.mailer import send_email_via_brevo_api


logger = get_logger("user", subfolder="routes")

user_bp = Blueprint("user", __name__)


@user_bp.before_request
def _require_ai_key_for_free():
    """Free plan = bring-your-own-key. A Free user with no AI key gets neither
    indexing nor answers (no platform fallback — €0 to the platform), so we gate
    the user surface until they configure a key. Pro/Enterprise/admin (platform
    Mistral included) are exempt. The settings + key endpoints stay reachable so
    the user can actually configure their key.
    """
    if not current_user.is_authenticated:
        return  # each view's @login_required handles authentication
    if current_user.is_pro_subscriber or current_user.has_ai_key:
        return
    allowed_endpoints = {
        "user.setup_ai_key",
        "user.account_settings",
        "user.set_ai_provider",
        "user.disable_ai_provider",
    }
    if request.endpoint in allowed_endpoints:
        return
    # Send the user to a dedicated, single-purpose "add your AI key" page
    # rather than dropping them on the busy settings page with a flash.
    return redirect(url_for("user.setup_ai_key"))


class _SearchPagination:
    """Minimal pagination object for in-memory search results, compatible with Flask-SQLAlchemy's paginate() interface."""

    def __init__(self, page, per_page, total):
        self.page = page
        self.per_page = per_page
        self.total = total
        self.pages = max(1, (total + per_page - 1) // per_page)
        self.has_prev = page > 1
        self.has_next = page < self.pages
        self.prev_num = page - 1 if self.has_prev else None
        self.next_num = page + 1 if self.has_next else None

    def iter_pages(self, left_edge=2, left_current=2, right_current=5, right_edge=2):
        last = 0
        for num in range(1, self.pages + 1):
            if (
                num <= left_edge
                or self.page - left_current - 1 < num < self.page + right_current
                or num > self.pages - right_edge
            ):
                if last + 1 != num:
                    yield None
                yield num
                last = num


@user_bp.route("/dashboard")
@login_required
def dashboard():
    if not current_user.is_admin and not current_user.is_subscriber:
        flash("Cette section est réservée aux abonnés.", "warning")
        return redirect(url_for("home.welcome_page"))

    index_stats = rag_service.get_index_stats()
    logger.debug("Dashboard user_id=%s chargé", current_user.id)

    from app.models.user_scraper import UserScraper
    scraper_count = UserScraper.query.filter_by(user_id=current_user.id).count()

    # Cloud document quota — counts every CloudDocument across the user's
    # connections, matched against their plan's max_files. Used by the
    # dashboard quota indicator (warning at 80%+, blocking at 100%).
    from app.models.cloud_connection import CloudDocument
    from app.services.user_library_service import get_limits
    user_limits = get_limits(current_user)
    docs_used = CloudDocument.query.filter_by(user_id=current_user.id).count()
    docs_max = user_limits["max_files"]
    docs_pct = round((docs_used / docs_max) * 100) if docs_max else 0

    org_doc_count = 0
    org_scraper_count = 0
    org_member_count = 0

    return render_template(
        "user/dashboard.html",
        index_stats=index_stats,
        scraper_count=scraper_count,
        org_doc_count=org_doc_count,
        org_scraper_count=org_scraper_count,
        org_member_count=org_member_count,
        docs_used=docs_used,
        docs_max=docs_max,
        docs_pct=docs_pct,
    )


@user_bp.route("/settings", methods=["GET", "POST"])
@login_required
def account_settings():
    if request.method == "POST":
        new_password = request.form.get("password")
        if new_password:
            if not is_strong_password(new_password):
                logger.warning("Échec mise à jour mot de passe : trop faible user_id=%s", current_user.id)
                flash(
                    "Le mot de passe doit contenir au moins 8 caractères, "
                    "une majuscule, une minuscule, un chiffre et un caractère spécial.",
                    "danger",
                )
                return redirect(url_for("user.account_settings"))

            current_user.set_password(new_password)
            logger.info("Mot de passe mis à jour user_id=%s", current_user.id)

        firstname = request.form.get("firstname", "").strip()
        lastname = request.form.get("lastname", "").strip()
        current_user.firstname = firstname or None
        current_user.lastname = lastname or None

        # Per-user digest AI summary opt-in (checkbox → present only when ticked).
        current_user.digest_summarize = bool(request.form.get("digest_summarize"))

        try:
            db.session.commit()
            flash("Préférences mises à jour.", "success")
        except Exception as e:
            db.session.rollback()
            logger.error("Erreur commit préférences user_id=%s : %s", current_user.id, e)
            flash("Erreur lors de la mise à jour de vos préférences.", "danger")
        return redirect(url_for("user.account_settings"))

    from app.utils.ai_providers import ALLOWED_PROVIDERS, FREE_PROVIDERS, PROVIDER_DEFAULT_MODELS, PROVIDER_LABELS

    # Free users only see embed-capable providers (their key must index too);
    # Pro/Enterprise/admin can pick any, incl. Claude (platform Mistral indexes).
    providers = ALLOWED_PROVIDERS if current_user.is_pro_subscriber else FREE_PROVIDERS
    return render_template(
        "user/settings.html",
        user=current_user,
        ai_providers=providers,
        ai_provider_labels=PROVIDER_LABELS,
        ai_provider_models=PROVIDER_DEFAULT_MODELS,
    )


@user_bp.route("/setup/ai-key")
@login_required
def setup_ai_key():
    """Dedicated single-purpose onboarding page.

    A Free user with no AI key is sent here by ``_require_ai_key_for_free``
    before they can reach the app — a clean "add your key" screen rather than
    the busy settings page. Only embed-capable providers are offered (the key
    must power indexing too). Pro/Enterprise/admin never need this page.
    """
    if current_user.is_pro_subscriber or current_user.has_ai_key:
        return redirect(url_for("user.dashboard"))
    from app.utils.ai_providers import FREE_PROVIDERS, PROVIDER_DEFAULT_MODELS, PROVIDER_LABELS
    return render_template(
        "user/setup_ai_key.html",
        ai_providers=FREE_PROVIDERS,
        ai_provider_labels=PROVIDER_LABELS,
        ai_provider_models=PROVIDER_DEFAULT_MODELS,
    )


@user_bp.route("/settings/ai-provider", methods=["POST"])
@login_required
def set_ai_provider():
    """Per-user BYOK: store the user's own LLM provider key (encrypted).

    Mirrors org_set_ai_provider but at the individual-account level. On the
    Free tier this is the ONLY way to get AI synthesis; Pro/Enterprise use it
    to override the included platform Mistral.
    """
    from app.utils.ai_providers import ALLOWED_PROVIDERS, FREE_PROVIDERS, PROVIDER_DEFAULT_MODELS, PROVIDER_LABELS
    from app.utils.crypto import encrypt_token
    from app.utils.deployment_mode import is_sovereign

    if is_sovereign():
        flash(
            "Cette plateforme est en mode souverain : aucun fournisseur IA "
            "externe n'est autorisé.",
            "warning",
        )
        return redirect(url_for("user.account_settings"))

    provider = request.form.get("provider", "").strip()
    api_key = request.form.get("api_key", "").strip()
    model = request.form.get("model", "").strip() or None

    if provider not in ALLOWED_PROVIDERS:
        flash("Fournisseur non reconnu.", "danger")
        return redirect(url_for("user.account_settings"))
    # Free: the key must also index → only embed-capable providers. Claude
    # (no embedding API) is Pro-only.
    if not current_user.is_pro_subscriber and provider not in FREE_PROVIDERS:
        flash(
            "En Free, votre clé doit aussi indexer vos documents — choisissez "
            "Mistral, OpenAI ou Gemini (Anthropic Claude est réservé au Pro).",
            "danger",
        )
        return redirect(url_for("user.account_settings"))
    if not api_key:
        flash("Clé API requise.", "danger")
        return redirect(url_for("user.account_settings"))

    current_user.ai_provider = provider
    current_user.ai_api_key_enc = encrypt_token(api_key)
    current_user.ai_model = model
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error("Erreur enregistrement fournisseur IA user_id=%s : %s", current_user.id, e)
        flash("Erreur lors de l'enregistrement de votre clé API.", "danger")
        return redirect(url_for("user.account_settings"))

    # The embed resolver may derive its key from this synthesis key (Mistral/
    # OpenAI do both) — drop its cache so the change takes effect immediately.
    try:
        from app.services.rag_service import invalidate_user_embed_resolution
        invalidate_user_embed_resolution(current_user.id)
    except Exception:
        pass

    logger.info(
        "User %s set AI provider %s (model=%s)",
        current_user.id, provider, model or PROVIDER_DEFAULT_MODELS.get(provider),
    )
    flash(
        f"Fournisseur IA « {PROVIDER_LABELS[provider]} » activé avec votre clé.",
        "success",
    )
    # Onboarding flow (dedicated setup page) → drop the user straight on the
    # dashboard now that they're no longer gated.
    if request.form.get("redirect_to") == "dashboard":
        return redirect(url_for("user.dashboard"))
    return redirect(url_for("user.account_settings"))


@user_bp.route("/settings/ai-provider/disable", methods=["POST"])
@login_required
def disable_ai_provider():
    """Remove the user's stored BYOK key."""
    current_user.ai_provider = None
    current_user.ai_api_key_enc = None
    current_user.ai_model = None
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error("Erreur désactivation fournisseur IA user_id=%s : %s", current_user.id, e)
        flash("Erreur lors de la suppression de votre clé API.", "danger")
        return redirect(url_for("user.account_settings"))

    try:
        from app.services.rag_service import invalidate_user_embed_resolution
        invalidate_user_embed_resolution(current_user.id)
    except Exception:
        pass

    logger.info("User %s disabled AI provider", current_user.id)
    flash("Votre clé API IA a été supprimée.", "success")
    return redirect(url_for("user.account_settings"))


@user_bp.route("/contact", methods=["GET", "POST"])
@login_required
def contact():
    form = ContactForm()

    if form.validate_on_submit():
        try:
            send_email_via_brevo_api(
                to="hello@myeline.io",
                subject="✅ Confirmation : message envoyé via le formulaire de contact",
                html_content=render_template(
                    "emails/email_admin_contact.html",
                    name=(
                        f"{current_user.firstname or ''} {current_user.lastname or ''}".strip()
                        or current_user.email
                    ),
                    email=current_user.email,
                    message=form.message.data,
                ),
            )
            logger.info("Message contact envoyé user_id=%s", current_user.id)
            flash("✅ Votre message a bien été envoyé à l'équipe Myeline.", "success")
        except Exception as e:
            logger.error("Échec envoi message contact : %s", e)
            flash("Une erreur est survenue lors de l'envoi du message.", "danger")
        return redirect(url_for("user.contact"))
    return render_template("user/contact.html", form=form)


@user_bp.route("/categories")
@login_required
def categories():
    if not current_user.is_admin and not current_user.is_subscriber:
        flash("Cette section est réservée aux abonnés.", "warning")
        return redirect(url_for("home.welcome_page"))

    newsletters = (
        Newsletter.query.filter(Newsletter.status == "sent")
        .options(joinedload(Newsletter.sections))
        .order_by(Newsletter.date.desc())
        .all()
    )

    sections_by_newsletter = {nl.id: getattr(nl, "sections", []) for nl in newsletters}

    logger.debug("Affichage catégories user_id=%s : %d newsletters", current_user.id, len(newsletters))

    return render_template(
        "user/categories.html",
        newsletters=newsletters,
        sections_by_newsletter=sections_by_newsletter,
    )
