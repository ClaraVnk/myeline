"""
Contrôle d'accès — anonymes redirigés, utilisateurs sans privilège bloqués,
admin autorisé sur les routes /admin/*.
"""
from tests.helpers import login


def test_anonymous_cannot_access_user_dashboard(client):
    r = client.get("/user/dashboard", follow_redirects=False)
    assert r.status_code in (301, 302)


def test_anonymous_cannot_access_admin_dashboard(client):
    r = client.get("/admin/dashboard", follow_redirects=False)
    assert r.status_code in (301, 302)


def test_anonymous_cannot_access_user_settings(client):
    r = client.get("/user/settings", follow_redirects=False)
    assert r.status_code in (301, 302)


def test_confirmed_user_cannot_access_admin_dashboard(client, confirmed_user):
    login(client, "alice@example.com", "Alice1234!")
    r = client.get("/admin/dashboard", follow_redirects=False)
    assert r.status_code in (301, 302)
    client.get("/logout")


def test_subscriber_can_access_user_dashboard(client, subscriber_user):
    login(client, "subscriber@example.com", "Alice1234!")
    r = client.get("/user/dashboard")
    assert r.status_code == 200
    client.get("/logout")


def test_confirmed_free_user_reaches_dashboard(client, confirmed_user):
    # Pivot Free/Pro : un compte gratuit confirmé & actif est désormais
    # "subscriber" pour l'accès de base — il atteint le dashboard (200),
    # il n'est plus redirigé par le gate global.
    login(client, "alice@example.com", "Alice1234!")
    r = client.get("/user/dashboard", follow_redirects=False)
    assert r.status_code == 200
    client.get("/logout")


def test_admin_can_access_admin_dashboard(client, admin_user):
    login(client, "admin@example.com", "Admin1234!")
    r = client.get("/admin/dashboard")
    assert r.status_code == 200
    client.get("/logout")


def test_admin_can_access_manage_users(client, admin_user):
    login(client, "admin@example.com", "Admin1234!")
    r = client.get("/admin/manage_users")
    assert r.status_code == 200
    client.get("/logout")
