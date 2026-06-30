"""Tests for user_rag routes — rag_search_page, rag_query."""
import json
from unittest.mock import MagicMock, patch

from tests.helpers import login


def _login_subscriber(client):
    login(client, "subscriber@example.com", "Alice1234!")


# ── rag_search_page ───────────────────────────────────────────────────────────


def test_rag_search_page(client, subscriber_user):
    """GET /user/recherche redirects to dashboard."""
    _login_subscriber(client)
    r = client.get("/user/recherche", follow_redirects=False)
    assert r.status_code in (301, 302)
    client.get("/logout")


def test_rag_search_page_requires_login(client):
    """Unauthenticated request redirects."""
    r = client.get("/user/recherche", follow_redirects=False)
    assert r.status_code in (301, 302)


# ── rag_query ─────────────────────────────────────────────────────────────────


def test_rag_query_empty_question(client, subscriber_user):
    """Empty query → 400 JSON error."""
    _login_subscriber(client)
    r = client.post(
        "/user/recherche/query",
        data=json.dumps({"query": ""}),
        content_type="application/json",
    )
    assert r.status_code == 400
    assert "error" in r.get_json()
    client.get("/logout")


def test_rag_query_too_long(client, subscriber_user):
    """Query longer than 400 chars → 400 JSON error."""
    _login_subscriber(client)
    r = client.post(
        "/user/recherche/query",
        data=json.dumps({"query": "x" * 401}),
        content_type="application/json",
    )
    assert r.status_code == 400
    client.get("/logout")


def test_rag_query_success(client, subscriber_user):
    """Valid query → enqueues job and returns job_id."""
    _login_subscriber(client)
    mock_job = MagicMock()
    mock_job.id = "test-job-123"
    mock_queue = MagicMock()
    mock_queue.enqueue.return_value = mock_job
    with patch("app.routes.user_rag.get_queue", return_value=mock_queue):
        r = client.post(
            "/user/recherche/query",
            data=json.dumps({"query": "What is aspirin?"}),
            content_type="application/json",
        )
    assert r.status_code == 200
    assert r.get_json() == {"job_id": "test-job-123"}
    client.get("/logout")


def test_rag_query_with_tag_filter(client, subscriber_user):
    """Valid query with tag filter → enqueue is called."""
    _login_subscriber(client)
    mock_job = MagicMock()
    mock_job.id = "test-job-456"
    mock_queue = MagicMock()
    mock_queue.enqueue.return_value = mock_job
    with patch("app.routes.user_rag.get_queue", return_value=mock_queue):
        r = client.post(
            "/user/recherche/query",
            data=json.dumps({"query": "aspirin", "tag": "cardio"}),
            content_type="application/json",
        )
    assert r.status_code == 200
    assert "job_id" in r.get_json()
    mock_queue.enqueue.assert_called_once()
    client.get("/logout")


def test_rag_query_service_exception(client, subscriber_user):
    """Queue failure → 500."""
    _login_subscriber(client)
    with patch("app.routes.user_rag.get_queue", side_effect=Exception("redis down")):
        r = client.post(
            "/user/recherche/query",
            data=json.dumps({"query": "aspirin?"}),
            content_type="application/json",
        )
    assert r.status_code == 500
    client.get("/logout")
