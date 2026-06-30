"""Round 5 A4-H3 — PROXY_HOPS env drives ProxyFix x_for/x_proto/x_host.

The session-scoped `app` fixture is created with default PROXY_HOPS=1.
Here we re-create the app with different env values and assert the
middleware was configured accordingly. Each test fully tears down its
local app instance.
"""
import importlib


def _create_app_with_env(monkeypatch, **env):
    """Build a fresh Flask app with the given env overrides applied first."""
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    # Ensure the module reads the patched env at create_app() time.
    app_module = importlib.import_module("app")
    return app_module.create_app()


def test_proxy_hops_defaults_to_one(monkeypatch):
    monkeypatch.delenv("PROXY_HOPS", raising=False)
    flask_app = _create_app_with_env(monkeypatch)
    pf = flask_app.wsgi_app
    assert pf.x_for == 1
    assert pf.x_proto == 1
    assert pf.x_host == 1


def test_proxy_hops_honours_env_value(monkeypatch):
    flask_app = _create_app_with_env(monkeypatch, PROXY_HOPS="2")
    pf = flask_app.wsgi_app
    assert pf.x_for == 2
    assert pf.x_proto == 2
    assert pf.x_host == 2


def test_proxy_hops_clamped_to_max_ten(monkeypatch):
    flask_app = _create_app_with_env(monkeypatch, PROXY_HOPS="999")
    assert flask_app.wsgi_app.x_for == 10


def test_proxy_hops_negative_clamped_to_zero(monkeypatch):
    flask_app = _create_app_with_env(monkeypatch, PROXY_HOPS="-5")
    assert flask_app.wsgi_app.x_for == 0
