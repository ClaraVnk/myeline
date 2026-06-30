"""Coverage tests for app.utils.safe_http (SSRF guard, redirect handling, byte cap)."""
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.utils import safe_http
from app.utils.safe_http import (
    SSRFBlockedError,
    _is_blocked_ip,
    _resolve_pinned_ip,
    safe_get,
    safe_request,
    validate_url,
)


# ---------------------------------------------------------------------------
# _is_blocked_ip
# ---------------------------------------------------------------------------


def test_is_blocked_ip_metadata_literal():
    """The known cloud-metadata IPs must be flagged as blocked."""
    assert _is_blocked_ip("169.254.169.254") is True
    assert _is_blocked_ip("169.254.170.2") is True


def test_is_blocked_ip_loopback_and_private():
    """Loopback and RFC1918 ranges must be blocked."""
    assert _is_blocked_ip("127.0.0.1") is True
    assert _is_blocked_ip("10.0.0.1") is True
    assert _is_blocked_ip("192.168.1.1") is True
    assert _is_blocked_ip("169.254.0.5") is True  # link-local


def test_is_blocked_ip_public():
    """A public IPv4 like 8.8.8.8 is not blocked."""
    assert _is_blocked_ip("8.8.8.8") is False


def test_is_blocked_ip_non_ip_returns_false():
    """Hostname strings (non-IP literals) return False — DNS step handles them."""
    assert _is_blocked_ip("example.com") is False


def test_is_blocked_ip_unspecified():
    """0.0.0.0 (unspecified) must be blocked."""
    assert _is_blocked_ip("0.0.0.0") is True


# ---------------------------------------------------------------------------
# _resolve_pinned_ip
# ---------------------------------------------------------------------------


def test_resolve_pinned_ip_returns_first_public(monkeypatch):
    """Returns first IP that is not in a blocked range."""
    fake_infos = [
        (None, None, None, None, ("127.0.0.1", 0)),
        (None, None, None, None, ("8.8.8.8", 0)),
    ]
    monkeypatch.setattr(safe_http.socket, "getaddrinfo", lambda h, p: fake_infos)
    assert _resolve_pinned_ip("anything") == "8.8.8.8"


def test_resolve_pinned_ip_all_blocked_raises(monkeypatch):
    """SSRFBlockedError when every resolved address is in a blocked range."""
    fake_infos = [
        (None, None, None, None, ("127.0.0.1", 0)),
        (None, None, None, None, ("10.0.0.1", 0)),
    ]
    monkeypatch.setattr(safe_http.socket, "getaddrinfo", lambda h, p: fake_infos)
    with pytest.raises(SSRFBlockedError, match="blocked range"):
        _resolve_pinned_ip("evil.example")


def test_resolve_pinned_ip_dns_failure_raises(monkeypatch):
    """getaddrinfo gaierror must surface as SSRFBlockedError."""
    import socket as _socket

    def boom(*_a, **_k):
        raise _socket.gaierror("nope")

    monkeypatch.setattr(safe_http.socket, "getaddrinfo", boom)
    with pytest.raises(SSRFBlockedError, match="Cannot resolve"):
        _resolve_pinned_ip("doesnotresolve.example")


# ---------------------------------------------------------------------------
# validate_url
# ---------------------------------------------------------------------------


def test_validate_url_rejects_non_string():
    """None / non-string URL must be rejected."""
    with pytest.raises(SSRFBlockedError, match="missing or not a string"):
        validate_url(None)  # type: ignore[arg-type]


def test_validate_url_rejects_empty():
    """Empty string URL must be rejected."""
    with pytest.raises(SSRFBlockedError):
        validate_url("")


def test_validate_url_rejects_unsupported_scheme():
    """ftp:// and friends are not allowed."""
    with pytest.raises(SSRFBlockedError, match="scheme"):
        validate_url("ftp://example.com/foo")


def test_validate_url_rejects_userinfo():
    """user:pass@host URLs must be rejected to prevent credential leaks."""
    with pytest.raises(SSRFBlockedError, match="userinfo"):
        validate_url("https://alice:secret@example.com/")


def test_validate_url_rejects_missing_host():
    """URL without a host must be rejected."""
    with pytest.raises(SSRFBlockedError, match="missing hostname"):
        validate_url("http:///path")


def test_validate_url_rejects_literal_blocked_ip():
    """URL with a literal blocked IP fails before DNS resolution."""
    with pytest.raises(SSRFBlockedError, match="blocked range"):
        validate_url("http://127.0.0.1/foo")


def test_validate_url_returns_components(monkeypatch):
    """A valid URL returns (scheme, host, port, pinned_ip)."""
    monkeypatch.setattr(
        safe_http, "_resolve_pinned_ip", lambda host: "8.8.8.8"
    )
    scheme, host, port, ip = validate_url("https://example.com/x")
    assert scheme == "https"
    assert host == "example.com"
    assert port == 443
    assert ip == "8.8.8.8"


def test_validate_url_default_http_port(monkeypatch):
    """http:// without explicit port resolves to port 80."""
    monkeypatch.setattr(safe_http, "_resolve_pinned_ip", lambda h: "8.8.8.8")
    _, _, port, _ = validate_url("http://example.com/")
    assert port == 80


def test_validate_url_explicit_port(monkeypatch):
    """Explicit port in URL is preserved."""
    monkeypatch.setattr(safe_http, "_resolve_pinned_ip", lambda h: "8.8.8.8")
    _, _, port, _ = validate_url("http://example.com:8080/")
    assert port == 8080


# ---------------------------------------------------------------------------
# safe_get / _safe_request — happy path, redirect, byte cap
# ---------------------------------------------------------------------------


def _make_response(*, status=200, body=b"hello", headers=None, is_redirect=False):
    """Build a fake requests.Response-like mock."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.headers = headers or {}
    resp.is_redirect = is_redirect
    resp.iter_content = MagicMock(return_value=iter([body])) if body else MagicMock(return_value=iter([]))
    resp.close = MagicMock()
    resp._content = b""
    return resp


def test_safe_get_happy_path(monkeypatch):
    """A 200 response is returned with truncated content cached on _content."""
    monkeypatch.setattr(safe_http, "validate_url", lambda u: ("https", "h", 443, "1.1.1.1"))

    fake_resp = _make_response(body=b"payload")
    fake_session = MagicMock()
    fake_session.request = MagicMock(return_value=fake_resp)
    monkeypatch.setattr(safe_http, "requests", fake_session)

    out = safe_get("https://example.com/x")
    assert out is fake_resp
    assert fake_resp._content == b"payload"
    fake_session.request.assert_called_once()
    kwargs = fake_session.request.call_args.kwargs
    assert kwargs["method"] == "GET"
    assert kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True


def test_safe_request_passes_method_data(monkeypatch):
    """safe_request forwards verb, body, json kwargs to underlying session."""
    monkeypatch.setattr(safe_http, "validate_url", lambda u: ("https", "h", 443, "1.1.1.1"))
    fake_resp = _make_response(body=b"ok")
    fake_session = MagicMock()
    fake_session.request = MagicMock(return_value=fake_resp)

    safe_request("post", "https://example.com/", json={"a": 1}, session=fake_session)

    call = fake_session.request.call_args.kwargs
    assert call["method"] == "POST"
    assert call["json"] == {"a": 1}


def test_safe_get_byte_cap_truncates(monkeypatch):
    """Bodies larger than max_bytes are truncated and the cap is logged."""
    monkeypatch.setattr(safe_http, "validate_url", lambda u: ("https", "h", 443, "1.1.1.1"))
    fake_resp = MagicMock(spec=requests.Response)
    fake_resp.status_code = 200
    fake_resp.headers = {}
    fake_resp.is_redirect = False
    fake_resp.iter_content = MagicMock(return_value=iter([b"a" * 8, b"b" * 8]))
    fake_resp.close = MagicMock()
    fake_resp._content = b""
    fake_session = MagicMock()
    fake_session.request = MagicMock(return_value=fake_resp)

    out = safe_get("https://example.com/x", max_bytes=10, session=fake_session)
    # 8 bytes accepted, then cap hits — partial chunk truncated to 2 bytes.
    assert len(out._content) == 10


def test_safe_get_follows_redirect(monkeypatch):
    """A 302 is followed once when allow_redirects=True; final URL is fetched."""
    calls = []

    def fake_validate(u):
        calls.append(u)
        return ("https", "h", 443, "1.1.1.1")

    monkeypatch.setattr(safe_http, "validate_url", fake_validate)

    redirect_resp = MagicMock(spec=requests.Response)
    redirect_resp.status_code = 302
    redirect_resp.is_redirect = True
    redirect_resp.headers = {"Location": "https://example.com/final"}
    redirect_resp.close = MagicMock()

    final_resp = _make_response(body=b"final")

    fake_session = MagicMock()
    fake_session.request = MagicMock(side_effect=[redirect_resp, final_resp])

    out = safe_get("https://example.com/start", session=fake_session)
    assert out is final_resp
    assert calls == ["https://example.com/start", "https://example.com/final"]
    assert fake_session.request.call_count == 2


def test_safe_get_redirect_without_location_raises(monkeypatch):
    """A 3xx without a Location header is treated as malformed and rejected."""
    monkeypatch.setattr(safe_http, "validate_url", lambda u: ("https", "h", 443, "1.1.1.1"))
    bad_redirect = MagicMock(spec=requests.Response)
    bad_redirect.status_code = 302
    bad_redirect.is_redirect = True
    bad_redirect.headers = {}
    bad_redirect.close = MagicMock()
    fake_session = MagicMock()
    fake_session.request = MagicMock(return_value=bad_redirect)

    with pytest.raises(SSRFBlockedError, match="Location"):
        safe_get("https://example.com/", session=fake_session)


def test_safe_get_too_many_redirects(monkeypatch):
    """More than _MAX_REDIRECTS hops aborts with SSRFBlockedError."""
    monkeypatch.setattr(safe_http, "validate_url", lambda u: ("https", "h", 443, "1.1.1.1"))

    def make_redirect(loc):
        r = MagicMock(spec=requests.Response)
        r.status_code = 302
        r.is_redirect = True
        r.headers = {"Location": loc}
        r.close = MagicMock()
        return r

    fake_session = MagicMock()
    fake_session.request = MagicMock(
        side_effect=[make_redirect(f"https://example.com/{i}") for i in range(safe_http._MAX_REDIRECTS + 2)]
    )

    with pytest.raises(SSRFBlockedError, match="Too many redirects"):
        safe_get("https://example.com/start", session=fake_session)


def test_safe_get_redirect_disabled_returns_3xx(monkeypatch):
    """allow_redirects=False yields the 3xx response as a final result."""
    monkeypatch.setattr(safe_http, "validate_url", lambda u: ("https", "h", 443, "1.1.1.1"))
    redirect_resp = MagicMock(spec=requests.Response)
    redirect_resp.status_code = 302
    redirect_resp.is_redirect = True
    redirect_resp.headers = {"Location": "https://elsewhere/"}
    redirect_resp.iter_content = MagicMock(return_value=iter([b""]))
    redirect_resp.close = MagicMock()
    redirect_resp._content = b""
    fake_session = MagicMock()
    fake_session.request = MagicMock(return_value=redirect_resp)

    out = safe_get("https://example.com/", allow_redirects=False, session=fake_session)
    assert out.status_code == 302


def test_safe_get_post_redirect_303_switches_to_get(monkeypatch):
    """303 redirect after POST becomes a GET on the next hop."""
    monkeypatch.setattr(safe_http, "validate_url", lambda u: ("https", "h", 443, "1.1.1.1"))

    redirect = MagicMock(spec=requests.Response)
    redirect.status_code = 303
    redirect.is_redirect = True
    redirect.headers = {"Location": "https://example.com/final"}
    redirect.close = MagicMock()

    final = _make_response(body=b"ok")
    fake_session = MagicMock()
    fake_session.request = MagicMock(side_effect=[redirect, final])

    safe_request("POST", "https://example.com/", data=b"x", session=fake_session)
    second = fake_session.request.call_args_list[1].kwargs
    assert second["method"] == "GET"
    assert second["data"] is None


def test_safe_get_timeout_capped(monkeypatch):
    """Caller-supplied timeout above _DEFAULT_TIMEOUT is clamped down."""
    monkeypatch.setattr(safe_http, "validate_url", lambda u: ("https", "h", 443, "1.1.1.1"))
    fake_resp = _make_response(body=b"x")
    fake_session = MagicMock()
    fake_session.request = MagicMock(return_value=fake_resp)

    safe_get("https://example.com/", timeout=9999, session=fake_session)
    used = fake_session.request.call_args.kwargs["timeout"]
    assert used == safe_http._DEFAULT_TIMEOUT
