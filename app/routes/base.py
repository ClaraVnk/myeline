from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

from app.forms.user_forms import ContactForm
from app.utils.logger import get_logger
from app.utils.recaptcha import verify_recaptcha


logger = get_logger("base", subfolder="routes")
logger.debug("Logger initialisé pour base routes.")


base_bp = Blueprint("base", __name__)


@base_bp.route("/contact", methods=["GET", "POST"])
def contact():
    form = ContactForm()
    if request.method == "POST":
        logger.info("Soumission du formulaire de contact détectée.")
        recaptcha_response = request.form.get("g-recaptcha-response")
        if not recaptcha_response:
            logger.warning("CAPTCHA non rempli.")
            flash("Veuillez valider le CAPTCHA.", "danger")
            return redirect(url_for("base.contact"))

        if not verify_recaptcha(recaptcha_response, "contact"):
            logger.error(
                "Échec de vérification reCAPTCHA v3 : échec de la validation "
                "(succès, action ou score)."
            )
            flash("Échec de la vérification reCAPTCHA. Veuillez réessayer.", "danger")
            return redirect(url_for("base.contact"))

    return render_template(
        "base/contact.html",
        form=form,
        site_key=current_app.config["RECAPTCHA_SITE_KEY"],
    )
