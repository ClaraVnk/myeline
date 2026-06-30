"""Coverage for app/services/user_library_service.py.

Direct file uploads are not exposed in any UI, so the upload
helpers (save_upload, save_document, delete_document, allowed_file,
index_document, remove_from_index) were removed in
commit "feat(library): drop dead upload code". What remains is the
per-plan limits table consulted by cloud_sync_utils.

OSS single-user build: paid tiers / org plans / subscription gating
were all removed. ``get_limits`` now returns the unlimited ("pro")
budget for every user, whatever their flags.
"""
from types import SimpleNamespace


def _user(*, is_admin=False, plan="gratuit"):
    return SimpleNamespace(
        id=1,
        is_admin=is_admin,
        abonnement_type=plan,
        is_pro_subscriber=True,  # always True in the OSS build
    )


class TestGetLimits:
    def test_any_user_gets_unlimited(self):
        from app.services.user_library_service import _LIMITS, get_limits
        assert get_limits(_user()) == _LIMITS["pro"]

    def test_admin_also_unlimited(self):
        from app.services.user_library_service import _LIMITS, get_limits
        assert get_limits(_user(is_admin=True)) == _LIMITS["pro"]

    def test_premium_user_unlimited(self):
        from app.services.user_library_service import _LIMITS, get_limits
        assert get_limits(_user(plan="premium_pro")) == _LIMITS["pro"]
