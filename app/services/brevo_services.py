import logging

import requests

from app.utils.mailer import _get_cfg


logger = logging.getLogger("brevo_services")


def get_email_events_by_message_id(message_id: str):
    """Récupère les événements d'un email spécifique via son messageId Brevo.

    Args:
        message_id (str): L'identifiant unique du message Brevo

    Returns:
        dict: Événements de l'email (delivered, opened, clicked, etc.)
              ou None en cas d'erreur
    """
    api_key = _get_cfg("BREVO_API_KEY")
    if not api_key:
        raise RuntimeError("Clé BREVO_API_KEY non définie")

    url = f"https://api.brevo.com/v3/smtp/emails/{message_id}"
    headers = {
        "accept": "application/json",
        "api-key": api_key,
    }

    response = None
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        # Re-raise l'exception pour permettre le retry en cas de rate limit
        if response and response.status_code == 429:
            raise
        logger.error(f"Erreur récupération événements pour messageId {message_id}: {e}")
        return None
