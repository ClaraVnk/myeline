"""Outbound HTTP wrapper with SSRF-prevention.

Audit 2026-05-06 finding C1: every user-supplied URL fetched by Myeline
(scraper feeds, WebDAV check, S3 endpoint test, Notion connector,
sitemap discovery, etc.) bypassed any IP-allowlist. Concrete attacks:

  * AWS / GCP / Azure cloud-metadata endpoints
    (`http://169.254.169.254/`, `http://metadata.google.internal/`).
  * Internal services on the podman bridge network
    (`http://mariadb:3306`, `http://redis:6379`, `http://ollama:11434`,
    even `http://web:5000/admin/...`).
  * Localhost on the host (`http://127.0.0.1/`).
  * DNS-rebind: domain resolves first to a public IP (passes the
    pre-fetch check), then to 127.0.0.1 for the actual fetch.

The fix is a single chokepoint that every outbound HTTP call from
user-supplied URLs flows through:

  * Restricts scheme to http/https.
  * Resolves the hostname and rejects any IP in private/loopback/
    link-local/reserved ranges, plus the explicit metadata-service IPs.
  * Pins the resolved IP for the actual fetch (defeats DNS rebind).
  * Caps the response body size.
  * Manually walks redirects so each hop re-passes the IP check.

This module is the only place that should be calling requests.* on a
user-supplied URL. Internal HTTP (Mistral / Anthropic / Stripe / Brevo
calls to known SaaS) keep their own clients — they don't touch user
input and live in the audited paths.
"""

import ipaddress
import socket
import threading
from contextlib import contextmanager
from typing import Optional
from urllib.parse import urlparse

import requests
import urllib3.util.connection as _urllib3_conn

from app.utils.logger import get_logger


logger = get_logger("safe_http", subfolder="utils")


# ─────────────────────────────────────────────────────────────────
# DNS pinning to defeat rebind attacks
# ─────────────────────────────────────────────────────────────────
#
# Bug H1 — Without this, validate_url() resolved the hostname to a
# safe public IP, then sess.request(url=…) handed the URL back to
# urllib3 which re-ran DNS at connection time. An attacker-controlled
# domain returning a public IP at the first lookup and 127.0.0.1 at the
# second (DNS rebind) defeated every SSRF guard.
#
# Fix: monkey-patch urllib3's create_connection once at import. Each
# safe_request enters a `_pin_dns(host, ip)` context that puts the
# (host → pinned IP) entry into a thread-local map. When urllib3
# resolves the hostname, our patched function intercepts and returns a
# socket connected to the pinned IP instead. Original hostname stays
# in the URL for SNI + Host header, so TLS validation works correctly.

_pin_local = threading.local()
_orig_create_connection = _urllib3_conn.create_connection


def _pinned_create_connection(address, *args, **kwargs):
    """urllib3.create_connection override — substitute pinned IPs."""
    host, port = address[0], address[1]
    pin_map = getattr(_pin_local, "map", None) or {}
    pinned = pin_map.get(host)
    if pinned:
        address = (pinned, port) + tuple(address[2:])
    return _orig_create_connection(address, *args, **kwargs)


# Install the patch once at module import. Idempotent (`is`-check
# prevents double-wrap if multiple workers re-import this module).
if _urllib3_conn.create_connection is _orig_create_connection:
    _urllib3_conn.create_connection = _pinned_create_connection


@contextmanager
def _pin_dns(host: str, ip: str):
    """Map `host` → `ip` for the duration of this thread's context."""
    if not hasattr(_pin_local, "map"):
        _pin_local.map = {}
    prev = _pin_local.map.get(host)
    _pin_local.map[host] = ip
    try:
        yield
    finally:
        if prev is None:
            _pin_local.map.pop(host, None)
        else:
            _pin_local.map[host] = prev


# Cloud-metadata addresses that should never be reachable from a server-
# side fetch even if the deployment doesn't run on the corresponding
# cloud — defence-in-depth for misconfigured proxies that route 169.254.*
# back into the application.
_BLOCKED_IPS = frozenset({
    "169.254.169.254",   # AWS / GCP / Azure / OpenStack metadata
    "169.254.170.2",     # AWS ECS task metadata
    "fd00:ec2::254",     # AWS IMDS over IPv6
    "fe80::a9fe:a9fe",   # link-local IPv6 metadata variants
})


# Conservative size cap for any single response body. RSS feeds, OPML,
# JSON manifests, sitemap.xml — all should comfortably fit. A malicious
# server returning 5 GB of XML or HTML stops cold here.
_MAX_RESPONSE_BYTES = int(20 * 1024 * 1024)  # 20 MB


# How many redirects we'll follow manually. Set low — a legitimate fetch
# very rarely needs more than 2-3 hops, an exfiltration attempt loves to
# bounce off many.
_MAX_REDIRECTS = 5


# Per-request hard timeout (connect, read). Any specific caller can pass
# a tighter timeout — we never raise it above this here.
_DEFAULT_TIMEOUT = 30


class SSRFBlockedError(ValueError):
    """Raised when a target URL resolves to a forbidden destination."""


def _is_blocked_ip(ip_str: str) -> bool:
    """True if this string is an IP literal that falls into a forbidden
    range. Returns False for non-IP strings (hostnames). The hostname
    case is handled by `_resolve_pinned_ip` which DNS-resolves first.
    """
    if ip_str in _BLOCKED_IPS:
        return True
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        # Not an IP literal — caller will resolve via DNS.
        return False
    # Bug CC2 — `is_loopback`/`is_private` ne couvrent PAS les IPv4-mapped
    # IPv6 (`::ffff:127.0.0.1` → loopback IPv4 sous une enveloppe IPv6).
    # Normaliser avant le check. Idem 6to4 (`2002::/16`) qui encapsule
    # une IPv4 routable mais peut pointer vers du privé via la partie
    # encapsulée — Python expose `sixtofour` qui retourne l'IPv4 encodée.
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
    # Block every special-use range RFC 5735 / 6890 lists.
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_pinned_ip(host: str) -> str:
    """Resolve host once, return one allowed IP, raise if all are blocked.

    The pinned IP is what the actual TCP connection will target — bypasses
    the second-resolution step that would otherwise let DNS rebinding
    return a private address.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SSRFBlockedError(f"Cannot resolve host {host!r}: {exc}") from exc

    seen = set()
    for family, _socktype, _proto, _canon, sockaddr in infos:
        ip = sockaddr[0]
        if ip in seen:
            continue
        seen.add(ip)
        if not _is_blocked_ip(ip):
            return ip
    raise SSRFBlockedError(
        f"All resolved addresses for {host!r} are in a blocked range "
        f"(private/loopback/metadata): {sorted(seen)}"
    )


def validate_url(url: str) -> tuple[str, str, int, str]:
    """Validate the URL and return (scheme, host, port, pinned_ip).

    Raises SSRFBlockedError on any of:
      * scheme not in {http, https}
      * host missing
      * host resolves to a blocked IP (private, loopback, link-local,
        cloud-metadata, etc.)
      * userinfo embedded in the URL (no `user:pass@host` allowed —
        cleans up surprise credential leaks via redirects)
    """
    if not isinstance(url, str) or not url:
        raise SSRFBlockedError("URL missing or not a string")

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise SSRFBlockedError(f"URL scheme {scheme!r} not allowed")

    if parsed.username or parsed.password:
        raise SSRFBlockedError("userinfo (user:pass@) not allowed in URL")

    host = parsed.hostname
    if not host:
        raise SSRFBlockedError("URL missing hostname")

    # Reject literal blocked IPs early without DNS round-trip.
    if _is_blocked_ip(host):
        raise SSRFBlockedError(f"URL host {host!r} is in a blocked range")

    port = parsed.port or (443 if scheme == "https" else 80)
    pinned_ip = _resolve_pinned_ip(host)
    return scheme, host, port, pinned_ip


def safe_get(
    url: str, *, timeout: int = _DEFAULT_TIMEOUT,
    headers: Optional[dict] = None,
    max_bytes: int = _MAX_RESPONSE_BYTES,
    allow_redirects: bool = True,
    session: Optional[requests.Session] = None,
) -> requests.Response:
    """GET with SSRF protection, body cap, and bounded redirects.

    Prefer this over `requests.get()` whenever the URL comes from user
    input (scraper feed URL, OAuth redirect target, manifest fetch,
    Notion / WebDAV / S3 endpoint test, sitemap discovery, etc.). The
    response carries `.content` already truncated to `max_bytes`.

    `headers` is forwarded as-is. Pass `User-Agent` etc. there.
    `session` lets a caller reuse a pooled requests.Session — we still
    enforce all guards on each request that flows through.
    """
    return _safe_request("GET", url, timeout=timeout, headers=headers,
                         max_bytes=max_bytes,
                         allow_redirects=allow_redirects, session=session)


def safe_request(
    method: str, url: str, *, timeout: int = _DEFAULT_TIMEOUT,
    headers: Optional[dict] = None,
    data=None, json=None,
    max_bytes: int = _MAX_RESPONSE_BYTES,
    allow_redirects: bool = True,
    session: Optional[requests.Session] = None,
) -> requests.Response:
    """Generic verb wrapper. Same SSRF guards as safe_get."""
    return _safe_request(method.upper(), url, timeout=timeout, headers=headers,
                         data=data, json=json, max_bytes=max_bytes,
                         allow_redirects=allow_redirects, session=session)


def _safe_request(
    method: str, url: str, *, timeout: int,
    headers: Optional[dict],
    data=None, json=None,
    max_bytes: int,
    allow_redirects: bool,
    session: Optional[requests.Session],
) -> requests.Response:
    """Internal driver — manual redirect loop so each hop re-validates."""
    import time as _time
    timeout = min(int(timeout or 0) or _DEFAULT_TIMEOUT, _DEFAULT_TIMEOUT)
    sess = session or requests
    current_url = url
    payload_data = data
    payload_json = json

    # Bug M2 (audit round 2) — `timeout` côté requests est per-socket-op
    # (connect/read), pas un deadline global. Avec 5 redirects max et
    # `iter_content` qui tolère un serveur servant 1 octet juste avant
    # chaque expiration de read-timeout, un attaquant pouvait tenir une
    # connexion plusieurs minutes (slowloris). On capture un deadline
    # absolu = 2× le timeout configuré, et on coupe net si on le dépasse
    # (en redirect ou en streaming de la réponse).
    deadline = _time.monotonic() + (timeout * 2)

    # Some headers can contain sensitive info we don't want forwarded
    # across hops. requests defaults are fine for redirects, but with
    # manual handling we set them sparingly.
    request_headers = dict(headers or {})

    for hop in range(_MAX_REDIRECTS + 1):
        if _time.monotonic() > deadline:
            raise SSRFBlockedError(f"Total request deadline exceeded for {url!r}")
        # Validate this hop's URL — re-runs the IP check after each
        # redirect, defeating "302 → http://127.0.0.1/" chains.
        # Also pin DNS to the resolved IP for the actual connection
        # (cf. _pin_dns docstring — defeats DNS rebind attacks).
        _scheme, host, _port, pinned_ip = validate_url(current_url)

        with _pin_dns(host, pinned_ip):
            resp = sess.request(
                method=method,
                url=current_url,
                headers=request_headers,
                data=payload_data,
                json=payload_json,
                timeout=timeout,
                stream=True,
                allow_redirects=False,
            )

        if allow_redirects and resp.is_redirect and 300 <= resp.status_code < 400:
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                raise SSRFBlockedError("Redirect without Location header")
            # Resolve relative redirects against the previous URL.
            from urllib.parse import urljoin
            current_url = urljoin(current_url, location)
            # 303 + redirect body semantics: drop body for next hop.
            payload_data = None
            payload_json = None
            method_for_next = "GET" if resp.status_code in (301, 302, 303) else method
            method = method_for_next
            continue

        # Final response — truncate body to max_bytes.
        chunks = []
        read = 0
        try:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                # Bug M2 — check deadline aussi en cours de streaming
                # (slowloris dans le body envoyant 1 octet juste sous
                # le read-timeout).
                if _time.monotonic() > deadline:
                    logger.warning(
                        "safe_http: total deadline exceeded mid-stream for %s", current_url,
                    )
                    break
                if not chunk:
                    continue
                read += len(chunk)
                if read > max_bytes:
                    chunks.append(chunk[: max_bytes - (read - len(chunk))])
                    logger.warning(
                        "safe_http: response body capped at %d bytes for %s",
                        max_bytes, current_url,
                    )
                    break
                chunks.append(chunk)
        finally:
            resp.close()
        # Replace the response body with the truncated content. .content
        # / .text / .json() all consume from the cached _content attr.
        body = b"".join(chunks)
        resp._content = body  # noqa: SLF001 — intentional override
        return resp

    raise SSRFBlockedError(f"Too many redirects (>{_MAX_REDIRECTS}) starting at {url!r}")
