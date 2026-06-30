import os

from flask import request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address


def get_real_ip():
    """Extrait l'IP réelle du client derrière un reverse proxy (Pangolin, Nginx…).

    Lit X-Real-IP en premier (en-tête mono-valeur positionné par Pangolin),
    puis utilise request.remote_addr déjà corrigé par ProxyFix via X-Forwarded-For.
    """
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    return get_remote_address()


def conditional_limit(limit_string, key_func=None):
    """Apply rate limiting conditionally based on environment.

    `key_func` is forwarded to limiter.limit when supplied — useful to
    key on `current_user.id` for an authenticated endpoint instead of
    falling back to the per-IP default (which collapses NAT'd users).
    """

    def decorator(func):
        if os.getenv("TESTING"):
            return func
        kwargs = {"key_func": key_func} if key_func else {}
        return limiter.limit(limit_string, **kwargs)(func)

    return decorator


if (
    os.getenv("FLASK_ENV", "production") != "production"
    or os.getenv("TESTING")
    or os.getenv("DISABLE_RATE_LIMITING", "false").lower() == "true"
):

    class MockLimiter:
        def enabled(self):
            return False

        def init_app(self, app):
            pass

        def limit(self, limit_string, **kwargs):
            def decorator(func):
                return func
            return decorator

        def exempt(self, func):
            return func

    limiter = MockLimiter()
else:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")

    # Crée une instance unique du Limiter avec stockage Redis
    # Ne pas définir default_limits ici pour éviter les valeurs vides
    limiter = Limiter(
        key_func=get_real_ip,
        storage_uri=redis_url,
    )
