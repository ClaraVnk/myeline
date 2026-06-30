"""Tests for app/utils/deployment_mode.py — predicates + route gating
+ template context.

These tests verify the classic / sovereign / hybrid predicates behave
correctly and that on-prem deployments don't accidentally expose
social login or external-API features they shouldn't.
"""
import pytest


# ─── Predicate semantics ──────────────────────────────────────────────

class TestModePredicates:
    """The helpers must return the right values for every mode + back-compat."""

    def test_classic_default(self, monkeypatch):
        from app.utils.deployment_mode import get_deployment_mode, is_classic, is_hybrid, is_on_prem, is_sovereign
        monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
        monkeypatch.delenv("SOVEREIGN_MODE", raising=False)
        assert get_deployment_mode() == "classic"
        assert is_classic() is True
        assert is_sovereign() is False
        assert is_hybrid() is False
        assert is_on_prem() is False

    def test_explicit_classic(self, monkeypatch):
        from app.utils.deployment_mode import get_deployment_mode, is_classic
        monkeypatch.setenv("DEPLOYMENT_MODE", "classic")
        assert get_deployment_mode() == "classic"
        assert is_classic() is True

    def test_sovereign(self, monkeypatch):
        from app.utils.deployment_mode import get_deployment_mode, is_classic, is_hybrid, is_on_prem, is_sovereign
        monkeypatch.setenv("DEPLOYMENT_MODE", "sovereign")
        assert get_deployment_mode() == "sovereign"
        assert is_sovereign() is True
        assert is_classic() is False
        assert is_hybrid() is False
        assert is_on_prem() is True

    def test_hybrid(self, monkeypatch):
        from app.utils.deployment_mode import get_deployment_mode, is_classic, is_hybrid, is_on_prem, is_sovereign
        monkeypatch.setenv("DEPLOYMENT_MODE", "hybrid")
        assert get_deployment_mode() == "hybrid"
        assert is_hybrid() is True
        assert is_sovereign() is False
        assert is_classic() is False
        assert is_on_prem() is True

    def test_legacy_sovereign_mode_flag(self, monkeypatch):
        """SOVEREIGN_MODE=true (old flag) should still resolve to sovereign."""
        from app.utils.deployment_mode import get_deployment_mode, is_sovereign
        monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
        monkeypatch.setenv("SOVEREIGN_MODE", "true")
        assert get_deployment_mode() == "sovereign"
        assert is_sovereign() is True

    def test_unknown_mode_falls_back_to_classic(self, monkeypatch):
        from app.utils.deployment_mode import get_deployment_mode
        monkeypatch.setenv("DEPLOYMENT_MODE", "weird-mode")
        monkeypatch.delenv("SOVEREIGN_MODE", raising=False)
        assert get_deployment_mode() == "classic"


# ─── Feature predicates ───────────────────────────────────────────────

class TestFeaturePredicates:
    @pytest.mark.parametrize("mode,expected", [
        ("classic", True),
        ("sovereign", False),
        ("hybrid", True),
    ])
    def test_allows_external_apis(self, monkeypatch, mode, expected):
        from app.utils.deployment_mode import allows_external_apis
        monkeypatch.setenv("DEPLOYMENT_MODE", mode)
        assert allows_external_apis() is expected

    @pytest.mark.parametrize("mode,expected", [
        ("classic", False),
        ("sovereign", False),
        ("hybrid", True),
    ])
    def test_is_byok(self, monkeypatch, mode, expected):
        from app.utils.deployment_mode import is_byok
        monkeypatch.setenv("DEPLOYMENT_MODE", mode)
        assert is_byok() is expected

    @pytest.mark.parametrize("mode", ["classic", "sovereign", "hybrid"])
    def test_oss_constants_are_mode_independent(self, monkeypatch, mode):
        """The OSS single-user build hard-wires these predicates: no billing,
        no pricing page, no enterprise provisioning, always single-tenant."""
        from app.utils.deployment_mode import (
            allows_enterprise_provisioning,
            has_pricing_page,
            has_stripe,
            is_single_tenant_default,
        )
        monkeypatch.setenv("DEPLOYMENT_MODE", mode)
        assert has_stripe() is False
        assert has_pricing_page() is False
        assert allows_enterprise_provisioning() is False
        assert is_single_tenant_default() is True


# ─── Social login routes hard-disabled in on-prem ─────────────────────

class TestSocialLoginGating:
    @pytest.mark.parametrize("mode", ["sovereign", "hybrid"])
    @pytest.mark.parametrize("provider", ["google", "microsoft", "apple"])
    def test_social_login_returns_404_in_on_prem(
        self, client, monkeypatch, mode, provider,
    ):
        monkeypatch.setenv("DEPLOYMENT_MODE", mode)
        r = client.get(f"/auth/social/{provider}", follow_redirects=False)
        assert r.status_code == 404, f"/{provider} should 404 in {mode}"

    @pytest.mark.parametrize("mode", ["sovereign", "hybrid"])
    @pytest.mark.parametrize("provider", ["google", "microsoft", "apple"])
    def test_social_callback_returns_404_in_on_prem(
        self, client, monkeypatch, mode, provider,
    ):
        monkeypatch.setenv("DEPLOYMENT_MODE", mode)
        r = client.get(f"/auth/social/{provider}/callback")
        assert r.status_code == 404


# ─── Mailer behaviour change in sovereign ─────────────────────────────

class TestMailerSovereignKillSwitch:
    def test_mailer_logs_only_in_sovereign(self, app, monkeypatch):
        from app.utils.mailer import send_email_via_brevo_api

        monkeypatch.setenv("DEPLOYMENT_MODE", "sovereign")
        with app.app_context():
            ok = send_email_via_brevo_api(
                to="x@example.com", subject="t", html_content="<p>hi</p>",
            )
        assert ok is True  # success = logged successfully
