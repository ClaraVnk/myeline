import logging
import re

from flask import current_app
from itsdangerous import URLSafeTimedSerializer


# --- Logger configuration ---

logger = logging.getLogger("user_services")


def is_strong_password(password):
    """Check if a password meets security requirements.

    Args:
        password (str): The password to validate

    Returns:
        bool: True if password is strong (8+ chars, uppercase, lowercase, digit, special char)
    """
    logger.debug("Checking password strength.")
    if not password:
        logger.debug("Password is empty")
        return False
    # Bug M16 — sans flag ASCII, `\w` matche les accents (« é » comptait
    # comme alphanumérique) et `\d` matche les chiffres Unicode (arabe,
    # devanagari). Politique de mot de passe ambiguë : `e1A@` passait,
    # `é1A@` aussi mais via une autre classification. Classes ASCII
    # explicites pour avoir un critère sans surprise.
    result = (
        len(password) >= 8
        and re.search(r"[A-Z]", password, re.ASCII)
        and re.search(r"[a-z]", password, re.ASCII)
        and re.search(r"\d", password, re.ASCII)
        and re.search(r"[^A-Za-z0-9\s]", password)
    )
    logger.debug(f"Password strength result: {result}")
    return bool(result)


def generate_unsubscribe_token(user):
    """Generate a secure token for email unsubscription.

    Args:
        user (User): The user object to generate token for

    Returns:
        str: URL-safe timed token containing user_id
    """
    logger.info(f"Generating unsubscribe token for user_id={user.id}")
    s = URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="unsubscribe")
    token = s.dumps({"user_id": user.id})
    logger.debug(f"Generated token: {token}")
    return token
