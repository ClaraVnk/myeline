"""2FA bypass list — for OAuth-review test accounts only.

Some OAuth providers (Google, Microsoft, Dropbox) require a working
test account during their app-validation review. Their reviewer
automation cannot complete a TOTP challenge, so we maintain a small
allowlist of emails that skip the mandatory-2FA enforcement.

CSV of emails configured via the ``BYPASS_2FA_EMAILS`` env var. Empty
in production. NEVER add a real end-user email — bypassing 2FA for a
real customer defeats the protection.
"""
import os
from functools import lru_cache


@lru_cache(maxsize=1)
def _bypass_set_cached(raw: str) -> frozenset:
    """Bug L5 (audit round 2) — le set bypass était reconstruit à
    chaque call (parse CSV + split + lower). Bénin en coût mais bruit
    inutile, et signalait une non-intention de cache. Memoize sur la
    valeur brute de l'env (changer l'env requiert un restart, ce qui
    invalide le cache via process recycling)."""
    return frozenset(
        e.strip().lower()
        for e in (raw or "").split(",")
        if e.strip()
    )


def is_2fa_bypassed(email_or_user) -> bool:
    """Return True if the caller is allowed to skip 2FA.

    Accepts either an email string (legacy) or a User instance. Bug L8 —
    pour un User, on refuse le bypass si is_admin=True, même si l'email
    se trouve dans BYPASS_2FA_EMAILS : un opérateur qui ajoute par
    inadvertance un admin à la liste env ne doit jamais voir la 2FA admin
    désactivée. La forme str (legacy) reste autorisée mais devrait migrer.

    Comparison is lower-cased on both sides so a typo in the env value
    or in the user's stored email casing doesn't silently break the
    bypass.
    """
    if hasattr(email_or_user, "email"):
        if getattr(email_or_user, "is_admin", False):
            return False
        email = email_or_user.email
    else:
        email = email_or_user
    if not email:
        return False
    return email.lower() in _bypass_set_cached(os.getenv("BYPASS_2FA_EMAILS", ""))
