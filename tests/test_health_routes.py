"""Tests for /health endpoint — covers Redis failure, container detection."""
from unittest.mock import MagicMock, patch


def test_health_db_ok_redis_fail(client):
    """Redis down → redis check unhealthy."""
    with patch("redis.from_url", side_effect=Exception("redis down")):
        r = client.get("/health")
    assert r.status_code in (200, 503)
    data = r.get_json()
    assert "checks" in data
    assert data["checks"]["database"]["status"] == "healthy"
    assert data["checks"]["redis"]["status"] == "unhealthy"


def test_health_redis_ok(client):
    """Redis up → redis check healthy."""
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True
    with patch("redis.from_url", return_value=mock_redis):
        r = client.get("/health")
    assert r.status_code in (200, 503)
    data = r.get_json()
    assert data["checks"]["redis"]["status"] == "healthy"


def test_health_container_detected_dockerenv(client):
    """/.dockerenv present → container status running."""
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True
    with patch("redis.from_url", return_value=mock_redis), \
         patch("os.path.exists", side_effect=lambda p: p == "/.dockerenv"):
        r = client.get("/health")
    data = r.get_json()
    assert data["checks"]["container"]["status"] == "running"


def test_health_container_detected_podman(client):
    """/run/.containerenv present (Podman) → container status running."""
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True
    with patch("redis.from_url", return_value=mock_redis), \
         patch("os.path.exists", side_effect=lambda p: p == "/run/.containerenv"):
        r = client.get("/health")
    data = r.get_json()
    assert data["checks"]["container"]["status"] == "running"


def test_health_container_not_detected(client):
    """No container markers → container status unknown."""
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True
    with patch("redis.from_url", return_value=mock_redis), \
         patch("os.path.exists", return_value=False):
        r = client.get("/health")
    data = r.get_json()
    assert data["checks"]["container"]["status"] == "unknown"


def test_health_full_response_structure(client):
    """Response has status, environment, checks, version."""
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True
    with patch("redis.from_url", return_value=mock_redis):
        r = client.get("/health")
    data = r.get_json()
    assert "status" in data
    assert "environment" in data
    assert "version" in data
