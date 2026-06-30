"""Round 5 A4-H1 / A4-H2 — observability endpoints must not leak business intel.

/metrics: 401 from a public IP without a bearer token.
/health:  public IP gets aggregate-only payload (status field only);
          private IP or valid bearer gets the full detail.

The test client's default REMOTE_ADDR is 127.0.0.1 (loopback) which
counts as private — every test below explicitly overrides REMOTE_ADDR
to '203.0.113.1' (TEST-NET-3, guaranteed-non-private) to simulate a
public caller.
"""
from unittest.mock import MagicMock, patch


PUBLIC_IP = "8.8.8.8"  # Public — Python's ipaddress.is_private flags TEST-NET ranges
LOOPBACK_IP = "127.0.0.1"


def test_metrics_rejected_from_public_ip_without_key(client, monkeypatch):
    monkeypatch.delenv("OBSERVABILITY_API_KEY", raising=False)
    r = client.get("/metrics", environ_overrides={"REMOTE_ADDR": PUBLIC_IP})
    assert r.status_code == 401


def test_metrics_allowed_from_loopback_without_key(client, monkeypatch):
    monkeypatch.delenv("OBSERVABILITY_API_KEY", raising=False)
    r = client.get("/metrics", environ_overrides={"REMOTE_ADDR": LOOPBACK_IP})
    assert r.status_code == 200
    assert b"myeline_" in r.data


def test_metrics_allowed_with_valid_bearer(client, monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_API_KEY", "s3cret-token")
    r = client.get(
        "/metrics",
        environ_overrides={"REMOTE_ADDR": PUBLIC_IP},
        headers={"Authorization": "Bearer s3cret-token"},
    )
    assert r.status_code == 200


def test_metrics_rejected_with_wrong_bearer(client, monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_API_KEY", "s3cret-token")
    r = client.get(
        "/metrics",
        environ_overrides={"REMOTE_ADDR": LOOPBACK_IP},
        headers={"Authorization": "Bearer wrong-token"},
    )
    # Bearer takes precedence over the loopback fallback when configured.
    assert r.status_code == 401


def test_health_public_returns_aggregate_only(client, monkeypatch):
    monkeypatch.delenv("OBSERVABILITY_API_KEY", raising=False)
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True
    with patch("redis.from_url", return_value=mock_redis):
        r = client.get("/health", environ_overrides={"REMOTE_ADDR": PUBLIC_IP})
    assert r.status_code in (200, 503)
    data = r.get_json()
    assert set(data.keys()) == {"status"}
    assert data["status"] in {"healthy", "degraded", "unhealthy"}


def test_health_private_ip_returns_full_detail(client, monkeypatch):
    monkeypatch.delenv("OBSERVABILITY_API_KEY", raising=False)
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True
    with patch("redis.from_url", return_value=mock_redis):
        r = client.get("/health", environ_overrides={"REMOTE_ADDR": LOOPBACK_IP})
    data = r.get_json()
    assert "checks" in data
    assert "environment" in data


def test_health_bearer_overrides_public_ip(client, monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_API_KEY", "s3cret-token")
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True
    with patch("redis.from_url", return_value=mock_redis):
        r = client.get(
            "/health",
            environ_overrides={"REMOTE_ADDR": PUBLIC_IP},
            headers={"Authorization": "Bearer s3cret-token"},
        )
    data = r.get_json()
    assert "checks" in data


def test_healthz_unaffected_by_gate(client, monkeypatch):
    monkeypatch.delenv("OBSERVABILITY_API_KEY", raising=False)
    r = client.get("/healthz", environ_overrides={"REMOTE_ADDR": PUBLIC_IP})
    assert r.status_code == 200
    assert r.get_json()["status"] == "alive"
