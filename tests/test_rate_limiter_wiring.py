"""Regression guard for the rate-limiter wiring.

A security audit found two divergent ``Limiter`` instances: route decorators
(``@limiter.limit(...)`` in auth/search/user_2fa/user_scrapers/user_rag)
imported ``limiter`` from ``app.ext``, while ``app/__init__.py`` only ever
called ``init_app`` on the *other* instance defined in
``app.utils.flask_limiter``. A Limiter that is never ``init_app``'d enforces
nothing, so every per-route limit — including the anti-brute-force cap on
``/2fa/verify`` — was silently dead in production.

These tests pin the contract: there must be exactly one limiter object, shared
by everything, so the divergence cannot reappear.
"""

import app.ext as ext
import app.utils.flask_limiter as flask_limiter
from app.routes import auth, search, user_2fa, user_rag, user_scrapers


def test_ext_limiter_is_the_initialized_instance():
    """app.ext must re-export the same limiter that app/__init__ initializes."""
    assert ext.limiter is flask_limiter.limiter


def test_route_modules_share_the_single_limiter():
    """Every module decorating routes must reference the one shared limiter."""
    shared = flask_limiter.limiter
    for module in (auth, search, user_2fa, user_scrapers, user_rag):
        assert module.limiter is shared, (
            f"{module.__name__} uses a different limiter instance — "
            "its @limiter.limit decorators would not be enforced"
        )
