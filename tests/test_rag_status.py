"""Tests for RAG async status polling endpoint."""
from unittest.mock import MagicMock, patch

from tests.helpers import login


def _login_subscriber(client):
    login(client, "subscriber@example.com", "Alice1234!")


def _mock_redis_job(status, result=None, raise_on_fetch=False):
    """Return context managers for patching Redis and Job."""
    mock_redis = MagicMock()
    mock_job = MagicMock()
    mock_job.get_status.return_value = status
    mock_job.meta = {}  # rag_status reads meta.get("stage") for live progress
    mock_job.result = result

    if raise_on_fetch:
        fetch_side_effect = Exception("job not found")
    else:
        fetch_side_effect = None

    return mock_redis, mock_job, fetch_side_effect


# ── Auth guard ────────────────────────────────────────────────────────────────


def test_rag_status_requires_login(client):
    r = client.get("/user/recherche/status/some-job-id", follow_redirects=False)
    assert r.status_code in (301, 302)


# ── Status endpoint ───────────────────────────────────────────────────────────


def test_rag_status_not_found(client, subscriber_user):
    """Job not found → 404."""
    _login_subscriber(client)
    with patch("redis.Redis") as mock_redis_cls, \
         patch("rq.job.Job.fetch", side_effect=Exception("job not found")):
        mock_redis_cls.from_url.return_value = MagicMock()
        r = client.get("/user/recherche/status/nonexistent-id")
    assert r.status_code == 404
    assert r.get_json()["status"] == "not_found"
    client.get("/logout")


def test_rag_status_finished(client, subscriber_user):
    """Finished job → result returned."""
    _login_subscriber(client)
    mock_job = MagicMock()
    mock_job.get_status.return_value = "finished"
    mock_job.meta = {}  # rag_status reads meta.get("stage") for live progress
    mock_job.result = {"answer": "42", "sources": []}
    mock_job.args = (subscriber_user.id,)  # ownership check

    with patch("redis.Redis") as mock_redis_cls, \
         patch("rq.job.Job.fetch", return_value=mock_job):
        mock_redis_cls.from_url.return_value = MagicMock()
        r = client.get("/user/recherche/status/test-job-123")

    assert r.status_code == 200
    data = r.get_json()
    assert data["status"] == "finished"
    assert data["result"] == {"answer": "42", "sources": []}
    client.get("/logout")


def test_rag_status_queued(client, subscriber_user):
    """Queued job → status string returned."""
    _login_subscriber(client)
    mock_job = MagicMock()
    mock_job.get_status.return_value = "queued"
    mock_job.meta = {}  # rag_status reads meta.get("stage") for live progress
    mock_job.args = (subscriber_user.id,)  # ownership check

    with patch("redis.Redis") as mock_redis_cls, \
         patch("rq.job.Job.fetch", return_value=mock_job):
        mock_redis_cls.from_url.return_value = MagicMock()
        r = client.get("/user/recherche/status/test-job-123")

    assert r.status_code == 200
    assert r.get_json()["status"] == "queued"
    client.get("/logout")


def test_rag_status_foreign_job_returns_404(client, subscriber_user):
    """Job belonging to another user → 404 (no IDOR)."""
    _login_subscriber(client)
    mock_job = MagicMock()
    mock_job.get_status.return_value = "finished"
    mock_job.meta = {}  # rag_status reads meta.get("stage") for live progress
    mock_job.result = {"answer": "secret", "sources": []}
    mock_job.args = (subscriber_user.id + 9999,)  # different user

    with patch("redis.Redis") as mock_redis_cls, \
         patch("rq.job.Job.fetch", return_value=mock_job):
        mock_redis_cls.from_url.return_value = MagicMock()
        r = client.get("/user/recherche/status/other-user-job")

    assert r.status_code == 404
    client.get("/logout")


def test_rag_status_failed(client, subscriber_user):
    """Failed job → 500."""
    _login_subscriber(client)
    mock_job = MagicMock()
    mock_job.get_status.return_value = "failed"
    mock_job.meta = {}  # rag_status reads meta.get("stage") for live progress
    mock_job.args = (subscriber_user.id,)  # ownership check

    with patch("redis.Redis") as mock_redis_cls, \
         patch("rq.job.Job.fetch", return_value=mock_job):
        mock_redis_cls.from_url.return_value = MagicMock()
        r = client.get("/user/recherche/status/test-job-123")

    assert r.status_code == 500
    assert r.get_json()["status"] == "failed"
    client.get("/logout")


def test_rag_status_finished_with_error_result(client, subscriber_user):
    """Finished job whose result contains an error key → failed status."""
    _login_subscriber(client)
    mock_job = MagicMock()
    mock_job.get_status.return_value = "finished"
    mock_job.meta = {}  # rag_status reads meta.get("stage") for live progress
    mock_job.result = {"error": "Erreur interne."}
    mock_job.args = (subscriber_user.id,)  # ownership check

    with patch("redis.Redis") as mock_redis_cls, \
         patch("rq.job.Job.fetch", return_value=mock_job):
        mock_redis_cls.from_url.return_value = MagicMock()
        r = client.get("/user/recherche/status/test-job-123")

    assert r.status_code == 200
    data = r.get_json()
    assert data["status"] == "failed"
    client.get("/logout")
