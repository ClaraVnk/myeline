"""Round 5 wave 5 — M/L hygiene fixes.

A2-L2 — Dropbox path validation refuses traversal patterns.
A2-L4 — Notion rich_text is HTML-stripped before indexing.
A2-L6 — S3 boto3 client passes verify=True explicitly.
A4-L1 — /status renders a latency bucket, not a raw millisecond.
A4-L5 — Stripe webhook contention bumps a Prometheus counter.
"""
from unittest.mock import patch


# ── A2-L2 — Dropbox path validation ────────────────────────────────────


def test_dropbox_refuses_path_with_dotdot():
    from app.services.dropbox_service import _build_index_descriptor
    entry = {
        "path_lower": "/folder/../etc/passwd",
        "name": "passwd.pdf",
        "size": 100,
    }
    assert _build_index_descriptor(None, entry, {}) is None


def test_dropbox_refuses_path_with_control_char():
    from app.services.dropbox_service import _build_index_descriptor
    entry = {
        "path_lower": "/folder/file\x00.pdf",
        "name": "file.pdf",
        "size": 100,
    }
    assert _build_index_descriptor(None, entry, {}) is None


def test_dropbox_refuses_empty_path():
    from app.services.dropbox_service import _build_index_descriptor
    assert _build_index_descriptor(None, {"name": "x.pdf"}, {}) is None


# ── A2-L4 — Notion rich_text stripping ─────────────────────────────────


def test_notion_extract_rich_text_strips_html():
    from app.services.notion_service import _extract_rich_text
    rt = [
        {"plain_text": "<script>steal()</script>Hello "},
        {"plain_text": "<img onerror=alert(1)>World"},
    ]
    out = _extract_rich_text(rt)
    assert "<" not in out
    assert "script" not in out
    assert "Hello" in out
    assert "World" in out


def test_notion_extract_rich_text_empty_safe():
    from app.services.notion_service import _extract_rich_text
    assert _extract_rich_text([]) == ""


# ── A2-L6 — S3 verify=True ─────────────────────────────────────────────


def test_s3_build_client_passes_verify_true(app):
    from unittest.mock import MagicMock

    from app.services import s3_service

    fake_conn = MagicMock()
    with app.app_context():
        with patch.object(s3_service, "_get_credentials",
                          return_value=("ak", "sk", "https://s3.eu-west-3.amazonaws.com", "bucket")):
            with patch("boto3.client") as boto:
                s3_service._build_client(fake_conn)
                kwargs = boto.call_args.kwargs
                assert kwargs["verify"] is True


def test_s3_test_connection_passes_verify_true(app):
    from app.services import s3_service

    with app.app_context():
        with patch("boto3.client") as boto:
            s3_service.test_connection("ak", "sk", "", "bucket")
            kwargs = boto.call_args.kwargs
            assert kwargs["verify"] is True


# ── A4-L1 — Latency bucketing on /status ───────────────────────────────


def test_status_page_renders_bucket_not_raw_ms(client, db, app):
    from app.models.status_sample import StatusSample
    from app.utils.time_utils import utcnow

    with app.app_context():
        sample = StatusSample(
            sampled_at=utcnow(),
            db_status="ok", redis_status="ok", ollama_status="ok",
            mistral_status="ok", web_status="ok",
            web_response_ms=237,
        )
        db.session.add(sample)
        db.session.commit()
        sample_id = sample.id

    r = client.get("/status")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # The raw "237 ms" reading must not appear; the bucket label must.
    assert "237 ms" not in body
    assert "&lt; 250 ms" in body or "< 250 ms" in body

    with app.app_context():
        db.session.delete(db.session.get(StatusSample, sample_id))
        db.session.commit()
