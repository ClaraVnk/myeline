"""
Tests de fumée — les pages publiques répondent, le health check est correct.
Aucun utilisateur requis.
"""
import json


def test_health_is_accessible(client):
    # 200 = healthy, 503 = degraded but still responding; both are acceptable
    r = client.get("/health")
    assert r.status_code in (200, 503)


def test_health_json_has_status(client):
    r = client.get("/health")
    data = json.loads(r.data)
    assert "status" in data
    assert "checks" in data
    assert "database" in data["checks"]


def test_login_page_accessible(client):
    r = client.get("/login")
    assert r.status_code == 200


def test_register_page_accessible(client):
    r = client.get("/register")
    assert r.status_code == 200


def test_root_redirects(client):
    r = client.get("/")
    assert r.status_code in (301, 302)


def test_nonexistent_page_is_404(client):
    r = client.get("/cette-page-nexiste-pas-du-tout")
    assert r.status_code == 404
