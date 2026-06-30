"""Deployment-mode helpers — classic / sovereign / hybrid.

Centralises every "is this feature available in this deployment?" check
so we don't end up with `os.getenv("SOVEREIGN_MODE")` calls strewn
across the codebase. The mode is decided by the installer
(``scripts/install.sh``) and persisted in ``DEPLOYMENT_MODE`` in .env.

  classic   — default. All cloud connectors and external APIs enabled.

  sovereign — air-gap on-prem. No external API call (Mistral, Brevo,
              OAuth providers all gated off). Synthesis goes to a local
              Ollama. Mailer is forced to log-only.

  hybrid    — on-prem deployment + BYOK external APIs. The operator
              tunes Mistral/Anthropic/OpenAI/Gemini keys.

Usage::

    from app.utils.deployment_mode import is_sovereign, is_hybrid

    if is_sovereign():
        ...

The module reads from environment variables — not from Flask config —
so it works in cron jobs and worker processes too. The Flask context
processor (in app/__init__.py) re-exposes the same values to templates.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal


Mode = Literal["classic", "sovereign", "hybrid"]
_VALID_MODES: tuple[Mode, ...] = ("classic", "sovereign", "hybrid")


def _read_mode() -> Mode:
    """Resolve the active deployment mode.

    Priority order (highest first):
      1. DEPLOYMENT_MODE env var if set to a known value.
      2. SOVEREIGN_MODE=true → maps to 'sovereign' (back-compat).
      3. Default: 'classic'.

    The env vars are re-read on each call (no caching) because tests
    monkeypatch them and we want the change to take effect.
    """
    explicit = os.getenv("DEPLOYMENT_MODE", "").strip().lower()
    if explicit in _VALID_MODES:
        return explicit  # type: ignore[return-value]

    if os.getenv("SOVEREIGN_MODE", "").strip().lower() in ("true", "1", "yes"):
        return "sovereign"

    return "classic"


def get_deployment_mode() -> Mode:
    """Return the active mode as a string."""
    return _read_mode()


# ────────────────────────────────────────────────────────────────────
# Mode predicates — preferred over comparing strings at call sites.
# ────────────────────────────────────────────────────────────────────

def is_classic() -> bool:
    """SaaS multi-tenant on a VPS (myeline.io style)."""
    return _read_mode() == "classic"


def is_sovereign() -> bool:
    """Air-gap on-prem deployment. No external API call permitted."""
    return _read_mode() == "sovereign"


def is_hybrid() -> bool:
    """On-prem deployment with BYOK external APIs (sovereign-hybride)."""
    return _read_mode() == "hybrid"


def is_on_prem() -> bool:
    """Either sovereign or hybrid — i.e. running on the customer's infra,
    not on the central myeline.io instance."""
    return _read_mode() in ("sovereign", "hybrid")


# ────────────────────────────────────────────────────────────────────
# Feature predicates — ask the question, not the mode.
# ────────────────────────────────────────────────────────────────────

def has_stripe() -> bool:
    """Billing is removed in the OSS single-user build."""
    return False


def has_pricing_page() -> bool:
    """No pricing page in the OSS single-user build (no billing)."""
    return False


def allows_external_apis() -> bool:
    """Mistral / Brevo / Stripe / Google APIs — anything that calls out
    to a public Internet endpoint."""
    return not is_sovereign()


def is_byok() -> bool:
    """Bring-your-own-key deployment. Each org configures its own LLM
    provider keys; the platform doesn't ship with a default Mistral
    plan key. True for hybrid; false for classic (platform ships keys)
    and sovereign (no external APIs at all)."""
    return is_hybrid()


def is_managed_tenant() -> bool:
    """No multi-tenant provisioning in the OSS single-user build."""
    return False


def allows_enterprise_provisioning() -> bool:
    """No enterprise sub-tenant provisioning in the OSS build."""
    return False


def is_single_tenant_default() -> bool:
    """The OSS build is always single-tenant."""
    return True


def mailer_default_dry_run() -> bool:
    """Should the mailer default to log-only? True in sovereign (no
    Brevo); false in classic and hybrid (real Brevo if configured)."""
    return is_sovereign()


# Note: 'Powered by Myeline' branding is intentionally non-removable.
# No whitelabel mode — the brand stays on every install regardless of
# tier or features. If you want a custom-branded product, contact us
# about a separate OEM agreement (handled out-of-band, not via license).


# ────────────────────────────────────────────────────────────────────
# Cached lookup of mode at app boot (used by route guards)
# ────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def cached_mode_at_boot() -> Mode:
    """Mode read once at process boot; safe to use in module-level
    route definitions. Reset by ``cached_mode_at_boot.cache_clear()``
    if needed (typically in tests)."""
    return _read_mode()
