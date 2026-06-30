"""URL-safe slug helper, factored out to avoid duplication.

Auparavant : `_make_slug` (stripe_webhook.py) et `_make_org_slug`
(admin_enterprise.py) — même logique recopiée, dérive garantie au
prochain tweak côté l'un. Une seule source ici, importée par les deux.
"""
import re


def make_org_slug(text: str, fallback: str = "enterprise", max_len: int = 60) -> str:
    """Convert an arbitrary string into a URL-safe slug.

    - Lowercases
    - Replaces every non-alphanumeric run by a single hyphen
    - Strips leading/trailing hyphens
    - Truncates to max_len chars
    - Returns `fallback` if the result is empty after normalisation
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower().strip()).strip("-")
    return slug[:max_len] or fallback
