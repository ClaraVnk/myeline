import logging

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


logger = logging.getLogger(__name__)


def generate_confirmation_token(email):
    serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"])
    token = serializer.dumps(email, salt="email-confirm")
    return token


def confirm_token(token, expiration=3600):
    serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"])
    try:
        email = serializer.loads(token, salt="email-confirm", max_age=expiration)
        logger.info("Token successfully confirmed.")
    except SignatureExpired:
        logger.warning("Token signature expired.")
        return None
    except BadSignature:
        logger.error("Bad token signature.")
        return None
    return email
