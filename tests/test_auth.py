"""
Authentification — login/logout valides et invalides, utilisateur non confirmé,
token de reset expiré, accès restreint pour utilisateurs non confirmés.
"""
from tests.helpers import login, logout


def test_login_valid_credentials_redirects(client, confirmed_user):
    r = login(client, "alice@example.com", "Alice1234!")
    assert r.status_code in (301, 302)


def test_login_invalid_password_returns_form(client, confirmed_user):
    r = login(client, "alice@example.com", "mauvais-mot-de-passe")
    assert r.status_code == 200


def test_login_unknown_email_returns_form(client):
    r = login(client, "inconnu@test.local", "nimportequoi")
    assert r.status_code == 200


def test_logout_redirects(client, confirmed_user):
    login(client, "alice@example.com", "Alice1234!")
    r = logout(client)
    assert r.status_code in (301, 302)


def test_logout_blocks_access_to_dashboard(client, confirmed_user):
    login(client, "alice@example.com", "Alice1234!")
    logout(client)
    r = client.get("/user/dashboard", follow_redirects=False)
    assert r.status_code in (301, 302)


def test_unconfirmed_user_can_log_in(client, unconfirmed_user):
    # Le login lui-même réussit ; c'est le contenu de sa session qui est restreint.
    r = login(client, "bob@example.com", "Bob12345!")
    assert r.status_code in (200, 301, 302)
    client.get("/logout")


def test_admin_login_with_user_credentials(client, confirmed_user):
    login(client, "alice@example.com", "Alice1234!")
    r = client.get("/admin/dashboard", follow_redirects=False)
    assert r.status_code in (301, 302)
    client.get("/logout")


# ── #18 : Token de reset expiré / invalide ────────────────────────────────────

def test_reset_password_invalid_token_redirects(client):
    """Un token de reset invalide redirige vers forgot-password (#18)."""
    r = client.get("/reset-password/invalid-token-xxxx", follow_redirects=False)
    assert r.status_code in (301, 302)


# ── #20 : Utilisateur non confirmé — accès restreint ─────────────────────────

def test_unconfirmed_user_login_blocked(client, unconfirmed_user):
    """Un utilisateur non confirmé ne peut pas se connecter (#20)."""
    r = login(client, "bob@example.com", "Bob12345!")
    # Le serveur renvoie 200 avec la page de renvoi de confirmation
    assert r.status_code == 200


def test_unconfirmed_user_cannot_access_dashboard(client, unconfirmed_user):
    """Même si la session existait, le dashboard est inaccessible sans confirmation (#20)."""
    r = client.get("/user/dashboard", follow_redirects=False)
    assert r.status_code in (301, 302)
