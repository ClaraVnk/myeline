"""
Newsletter — admin dashboard view.
"""
from tests.helpers import login


def test_admin_can_view_newsletter_list(client, admin_user):
    login(client, "admin@example.com", "Admin1234!")
    r = client.get("/admin/dashboard")
    assert r.status_code == 200
    client.get("/logout")
