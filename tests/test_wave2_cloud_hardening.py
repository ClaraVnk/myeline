"""Round 5 wave 2 — cloud connector hardening.

A2-H1 — Zotero metadata is stripped of HTML before indexing.
A2-H2 — Zotero snapshot ZIP refuses oversize entries.
A2-H3 — token-refresh lock helper degrades safely without Redis.
A2-M5 — non-https web_url is rejected at the ingestion boundary.
"""
import zipfile
from io import BytesIO
from unittest.mock import patch


# ── A2-H1 — Zotero HTML stripping ──────────────────────────────────────


def test_zotero_build_text_strips_html_from_title():
    from app.services.zotero_service import _build_text
    data = {
        "title": "<img src=x onerror='alert(1)'>Real Title",
        "abstractNote": "<script>steal()</script>Plain text body",
        "tags": [{"tag": "<b>tagged</b>"}],
    }
    out = _build_text(data)
    assert "alert" not in out
    assert "script" not in out
    assert "<" not in out
    assert "Real Title" in out
    assert "Plain text body" in out
    assert "tagged" in out


def test_zotero_build_text_preserves_plain_creators():
    from app.services.zotero_service import _build_text
    data = {"title": "Paper", "creators": [{"firstName": "Ada", "lastName": "Lovelace"}]}
    out = _build_text(data)
    assert "Ada Lovelace" in out


# ── A2-H2 — Zotero snapshot decompression cap ──────────────────────────


def _make_zip(html_content: bytes) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("index.html", html_content)
    return buf.getvalue()


def test_zotero_snapshot_within_cap_extracts(tmp_path):
    from app.utils.text_extraction import _extract_zotero_snapshot
    zip_bytes = _make_zip(b"<html><body>Hello world</body></html>")
    p = tmp_path / "snap.zip"
    p.write_bytes(zip_bytes)
    out = _extract_zotero_snapshot(p)
    assert "Hello world" in out


def test_zotero_snapshot_refuses_oversize_html(tmp_path):
    from app.utils.text_extraction import _extract_zotero_snapshot
    big = b"a" * (11 * 1024 * 1024)  # 11 MB > 10 MB cap
    zip_bytes = _make_zip(big)
    p = tmp_path / "bomb.zip"
    p.write_bytes(zip_bytes)
    assert _extract_zotero_snapshot(p) == ""


def test_zotero_snapshot_refuses_oversize_archive(tmp_path):
    """Archive-level cap catches multi-file zip bombs even if no single file blows the per-file cap."""
    from app.utils.text_extraction import _extract_zotero_snapshot
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(10):
            zf.writestr(f"f{i}.bin", b"a" * (8 * 1024 * 1024))  # 8 MB × 10 = 80 MB > 50 MB cap
        zf.writestr("index.html", b"<html></html>")
    p = tmp_path / "bomb.zip"
    p.write_bytes(buf.getvalue())
    assert _extract_zotero_snapshot(p) == ""


# ── A2-H3 — refresh lock fail-safe ─────────────────────────────────────


def test_cloud_token_refresh_lock_yields_false_when_redis_down():
    from app.services.cloud_sync_utils import cloud_token_refresh_lock
    with patch("redis.from_url", side_effect=Exception("nope")):
        with cloud_token_refresh_lock(123) as acquired:
            assert acquired is False


def test_cloud_token_refresh_lock_acquires_when_redis_ok():
    """When Redis returns truthy on SETNX, the lock reports acquisition."""
    from unittest.mock import MagicMock

    from app.services.cloud_sync_utils import cloud_token_refresh_lock
    fake_r = MagicMock()
    fake_r.set.return_value = True
    fake_r.delete.return_value = 1
    with patch("redis.from_url", return_value=fake_r):
        with cloud_token_refresh_lock(123) as acquired:
            assert acquired is True
        # Released on exit.
        fake_r.delete.assert_called_once()


# ── A2-M5 — web_url scheme whitelist ────────────────────────────────────


def test_is_safe_web_url_accepts_https():
    from app.services.cloud_sync_utils import is_safe_web_url
    assert is_safe_web_url("https://drive.google.com/file/d/abc/view") is True


def test_is_safe_web_url_rejects_javascript():
    from app.services.cloud_sync_utils import is_safe_web_url
    assert is_safe_web_url("javascript:alert(1)") is False


def test_is_safe_web_url_rejects_data_url():
    from app.services.cloud_sync_utils import is_safe_web_url
    assert is_safe_web_url("data:text/html,<script>x</script>") is False


def test_is_safe_web_url_rejects_http():
    """Cloud providers always return https; http is suspicious enough to drop."""
    from app.services.cloud_sync_utils import is_safe_web_url
    assert is_safe_web_url("http://example.com/doc") is False


def test_is_safe_web_url_rejects_empty():
    from app.services.cloud_sync_utils import is_safe_web_url
    assert is_safe_web_url("") is False
    assert is_safe_web_url(None) is False
