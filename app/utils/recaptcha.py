"""Utility functions for reCAPTCHA verification."""

import os

import requests
from flask import current_app, request


def verify_recaptcha(token, expected_action):
    """Verify reCAPTCHA token with Google API.

    Args:
        token (str): The reCAPTCHA token to verify
        expected_action (str): The expected action for the token

    Returns:
        bool: True if verification succeeds, False otherwise
    """
    # Bug L6 (audit round 2) — l'ancien `FLASK_ENV != "production"`
    # désactivait reCAPTCHA pour staging/preview/anything-not-production,
    # exposant des envs publics aux bots. Whitelist stricte : seuls
    # `development` et `testing` désactivent. Un opérateur peut toujours
    # passer DISABLE_RECAPTCHA=true pour un cas légitime.
    flask_env = os.environ.get("FLASK_ENV", "production").lower()
    if (
        flask_env in ("development", "testing")
        or os.environ.get("DISABLE_RECAPTCHA", "false").lower() == "true"
    ):
        return True

    # Bug L3 — l'ancien code appelait resp.json() sans try/except. Si
    # Google répondait du HTML (panneau de maintenance) ou un 5xx, la
    # route auth (login/register/forgot-password) crashait en 500 visible
    # côté user (DoS partiel sur toute la chaîne auth). Fix : valider
    # status + encadrer le json + fail-closed.
    secret = current_app.config["RECAPTCHA_SECRET_KEY"]
    try:
        resp = requests.post(
            "https://www.google.com/recaptcha/api/siteverify",
            data={
                "secret": secret,
                "response": token,
                "remoteip": request.remote_addr,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        current_app.logger.warning(
            "verify_recaptcha: Google API unreachable / invalid response — failing closed",
        )
        return False

    return bool(
        data.get("success")
        and data.get("score", 0) >= 0.5
        and data.get("action") == expected_action
    )
