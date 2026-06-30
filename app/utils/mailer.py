import base64
import os
from typing import Optional

import requests
from flask import current_app

from app.utils.logger import get_logger


logger = get_logger(__name__)


def _get_cfg(key: str, default=None):
    """Récupère la config Flask si dispo, sinon la variable d'env."""
    if current_app and key in current_app.config:
        return current_app.config[key]
    return os.getenv(key, default)


def _dev_log_email(to: str, subject: str, html_content: str) -> None:
    """Log email content to the application log (dev / test mode)."""
    import re
    text = re.sub(r"<[^>]+>", " ", html_content).strip()
    logger.info(
        "\n" + "=" * 60 + "\n"
        "[DEV EMAIL] To: %s\n"
        "Subject: %s\n"
        "Body (text):\n%s\n"
        + "=" * 60,
        to, subject, text[:2000],
    )


def send_email_via_brevo_api(
    to: str,
    subject: str,
    html_content: str,
    attachment_path: Optional[str] = None,
):
    """Send an HTML email via the Brevo transactional API.

    Kill-switches (in order of precedence):

      1. MAIL_DRY_RUN=true            — explicit override that always logs,
                                        regardless of FLASK_ENV. Use this
                                        on a dev box that runs with
                                        FLASK_ENV=production for prod-like
                                        smoke tests.
      2. BREVO_API_KEY=dummy          — also always logs (test fixtures).
      3. FLASK_ENV != production      — logs by default unless
                                        MAIL_FORCE_SEND=true is set
                                        (staging that needs real delivery).
    """
    api_key = _get_cfg("BREVO_API_KEY")

    # Sovereign / air-gapped deployments must never reach Brevo. The
    # SaaS mailer is just inappropriate there — the customer expects
    # everything to stay on-prem. Log instead of sending.
    from app.utils.deployment_mode import is_sovereign
    if is_sovereign():
        logger.info("Sovereign mode — email logged instead of sent (Brevo not reached).")
        _dev_log_email(to, subject, html_content)
        return True

    # Hard override: explicit dry-run always wins.
    dry_run = str(_get_cfg("MAIL_DRY_RUN", "false")).lower() in ("true", "1", "yes")
    if dry_run:
        logger.warning("MAIL_DRY_RUN=true — logging email instead of sending.")
        _dev_log_email(to, subject, html_content)
        return True

    # Test-only bypass: dummy key always logs.
    if api_key == "dummy":
        logger.info("BREVO_API_KEY=dummy — skipping real send.")
        _dev_log_email(to, subject, html_content)
        return True

    flask_env = _get_cfg("FLASK_ENV", "")
    force_send = str(_get_cfg("MAIL_FORCE_SEND", "false")).lower() in ("true", "1", "yes")
    if flask_env != "production" and not force_send:
        logger.warning(
            "Mailer kill-switch active (FLASK_ENV=%s) — logging instead of sending. "
            "Set MAIL_FORCE_SEND=true to override.", flask_env or "unset",
        )
        _dev_log_email(to, subject, html_content)
        return True

    if not api_key:
        raise RuntimeError("BREVO_API_KEY is not set")

    sender_email = _get_cfg("MAIL_DEFAULT_SENDER", "hello@myeline.io")
    sender_name = _get_cfg("MAIL_SENDER_NAME", "Myeline")

    url = "https://api.brevo.com/v3/smtp/email"
    headers = {
        "accept": "application/json",
        "api-key": api_key,
        "content-type": "application/json",
    }
    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": to}],
        "subject": subject,
        "htmlContent": html_content,
    }

    if attachment_path:
        with open(attachment_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode()
        filename = os.path.basename(attachment_path)
        payload["attachment"] = [{"content": encoded, "name": filename}]

    # Bug H7 (audit round 2) — l'ancien code loggait `to=<email>` en
    # clair à chaque envoi, ce qui viole la règle "logs pseudonymized,
    # never email" du CLAUDE.md. On log un hash court de l'adresse
    # (8 hex), suffisant pour corréler entre logs / Brevo dashboard
    # sans exposer le PII.
    import hashlib
    to_hash = hashlib.sha256((to or "").lower().encode("utf-8")).hexdigest()[:8]
    try:
        logger.info("Sending email via Brevo to_hash=%s subject=%s", to_hash, subject)
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        logger.info("Email sent via Brevo to_hash=%s", to_hash)
        return response.json()
    except requests.RequestException as e:
        logger.error("Brevo send failed to_hash=%s: %s", to_hash, e)
        return False
