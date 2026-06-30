"""Bearer + network ACL gate for /metrics and /health detail.

Round 5 A4-H1 / A4-H2: keep Prometheus-style observability endpoints
working in private-network deployments while refusing to leak business
intel (user counts per tier, dependency fingerprint) to the public
Internet.

Two layers, evaluated in order:

1. If ``OBSERVABILITY_API_KEY`` is set, the caller MUST present
   ``Authorization: Bearer <key>``. Constant-time comparison. Mismatch
   or missing header → unauthorized.
2. Otherwise, the caller's source IP must be loopback / RFC1918 / link-local.
   Public IPs get aggregate-only responses.

The IP check is intentionally pessimistic: ``ProxyFix`` runs *before*
the request reaches the handler, so ``get_real_ip()`` already returns
the client IP — provided ``PROXY_HOPS`` is set correctly (see audit
A4-H3). A misconfigured proxy chain would make every external request
look like the proxy's IP, but the proxy is typically inside the
private range, so this fails *open* on misconfig rather than fails
closed. The bearer-token path exists precisely to avoid relying on
that assumption.
"""
import hmac
import ipaddress
import os

from flask import request


def _is_private_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except (ValueError, TypeError):
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def observability_authorized() -> bool:
    """True if the caller may see detailed observability output."""
    expected = os.environ.get("OBSERVABILITY_API_KEY", "").strip()
    if expected:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return hmac.compare_digest(auth[7:], expected)

    # No bearer configured → restrict to private network.
    from app.utils.flask_limiter import get_real_ip
    return _is_private_ip(get_real_ip() or "")
