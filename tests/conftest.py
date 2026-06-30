"""
Fixtures partagées — base SQLite en mémoire, pas de mocking.
Chaque test travaille sur une vraie app Flask avec une vraie DB.
"""

import os
from unittest.mock import MagicMock
from unittest.mock import patch as _stdlib_patch

import pytest
from sqlalchemy.pool import StaticPool


# Hard-set test-only env vars so a host running with FLASK_ENV=production
# doesn't leak its reCAPTCHA / rate-limiter / debug settings into the suite.
os.environ["TESTING"] = "true"
os.environ["DISABLE_RECAPTCHA"] = "true"
os.environ["DISABLE_RATE_LIMITING"] = "true"
os.environ["FLASK_ENV"] = "testing"
# Force SQLite for tests so the prod MySQL pool options (pool_timeout etc.)
# don't leak in via Config.SQLALCHEMY_ENGINE_OPTIONS, which selects MySQL
# pool args when DATABASE_URL is mysql+pymysql:// — incompatible with SQLite.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("SECURITY_PASSWORD_SALT", "test-salt")
os.environ.setdefault("MISTRAL_API_KEY", "test")
os.environ.setdefault("BREVO_API_KEY", "test")
# Real Fernet keys so EncryptedString columns (TOTP, OAuth tokens, BYOK) and
# encrypted file uploads work without depending on the host environment.
from cryptography.fernet import Fernet  # noqa: E402


os.environ.setdefault("CLOUD_TOKEN_KEY", Fernet.generate_key().decode())
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())

# Per-worker Redis DB to keep xdist workers from stepping on each other.
# Several code paths (stripe_webhook event dedup, sso_nonce, the cloud
# token refresh lock added in round 5) write to the real Redis. Without
# isolation, a key written by worker gw0 in test A can break worker gw1
# running test B — and the failure shifts depending on how xdist
# happens to distribute tests across runs (which is why CI failed with
# a different set of tests than local). PYTEST_XDIST_WORKER is set by
# xdist to "gwN"; in single-process mode the env var is absent and we
# default to DB 15 so we never collide with whatever happens to be on
# DB 0 (the conventional dev default).
_xdist_worker = os.environ.get("PYTEST_XDIST_WORKER", "")
if _xdist_worker.startswith("gw"):
    try:
        _redis_db = int(_xdist_worker[2:]) % 16
    except ValueError:
        _redis_db = 15
else:
    _redis_db = 15

_redis_base = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
if "/" in _redis_base.split("://", 1)[-1]:
    _redis_root = _redis_base.rsplit("/", 1)[0]
else:
    _redis_root = _redis_base
os.environ["REDIS_URL"] = f"{_redis_root}/{_redis_db}"


@pytest.fixture(autouse=True)
def _flush_redis_between_tests():
    """Wipe the test's Redis DB after each test.

    Pairs with the per-worker DB index above: each xdist worker has its
    own DB, and we flush it after every test so e.g. stripe:event:* dedup
    keys from one test don't pre-empt the next.
    """
    yield
    try:
        from redis import Redis

        Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=1).flushdb()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Stub out chromadb so tests never trigger the chroma-hnswlib import.
# ---------------------------------------------------------------------------
#
# The chroma-hnswlib wheel ships with AVX/AVX2 SIMD opcodes. GitHub
# Actions runners cycle between CPU classes; ones without those
# instructions abort the import with "Fatal Python error: Illegal
# instruction", which takes down the whole xdist worker. Source-rebuild
# from the runner doesn't reliably help (pip cache reuse + compiler
# defaults still emit -march=native equivalents).
#
# The five tests that crashed all reached _chroma_client via an
# unmocked code path (get_index_stats called from /user/dashboard,
# search() called from a test that already mocked the search internals
# but not the collection retrieval). Tests should not be loading the
# real PersistentClient — patch it once here at module scope so every
# code path gets a harmless MagicMock collection.
#
# Tests that genuinely exercise chromadb behaviour can opt out by
# patching app.services.rag_service._chroma_client with their own
# fixture; this stub only replaces the underlying client object, so
# higher-level mocks on _hybrid_query_collection / search / etc. keep
# working unchanged.


_chroma_stub_client = MagicMock(name="conftest-chroma-client-stub")
_chroma_stub_collection = MagicMock(name="conftest-chroma-collection-stub")
_chroma_stub_collection.count.return_value = 0
_chroma_stub_collection.query.return_value = {
    "ids": [[]],
    "documents": [[]],
    "metadatas": [[]],
    "distances": [[]],
}
_chroma_stub_collection.get.return_value = {
    "ids": [],
    "documents": [],
    "metadatas": [],
}
_chroma_stub_client.get_or_create_collection.return_value = _chroma_stub_collection
_chroma_stub_client.get_collection.return_value = _chroma_stub_collection
_chroma_stub_client.list_collections.return_value = []


def _make_chroma_stub_client(*args, **kwargs):
    return _chroma_stub_client


# Patch the underlying chromadb factory rather than _chroma_client itself
# so tests that intentionally patch chromadb.PersistentClient inside their
# own `with patch(...)` block (e.g. the _chroma_client caching unit test)
# still see their own patch take precedence.
_chromadb_patcher = _stdlib_patch(
    "chromadb.PersistentClient",
    side_effect=_make_chroma_stub_client,
)
_chromadb_patcher.start()

# macOS LibreSSL does not implement scrypt; fall back to pbkdf2:sha256
try:
    import hashlib

    hashlib.scrypt(b"x", salt=b"x", n=2, r=1, p=1)
except (AttributeError, OSError):
    from werkzeug import security as _wz

    _orig_hash = _wz.generate_password_hash

    def _pbkdf2_hash(password, method=None, salt_length=16):
        return _orig_hash(password, method="pbkdf2:sha256", salt_length=salt_length)

    _wz.generate_password_hash = _pbkdf2_hash


@pytest.fixture(scope="session")
def app():
    from app import create_app

    flask_app = create_app()
    flask_app.config.update(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "SQLALCHEMY_ENGINE_OPTIONS": {
                "connect_args": {"check_same_thread": False},
                "poolclass": StaticPool,
            },
            "WTF_CSRF_ENABLED": False,
            "SERVER_NAME": "localhost",
            "DISABLE_RECAPTCHA": True,
            "REDIS_URL": "redis://localhost:6379/0",
        }
    )

    from app.ext import db

    with flask_app.app_context():
        # Hard guard: refuse to create_all / drop_all against anything other
        # than an in-memory SQLite engine. If the bind URL ever leaks through
        # to MySQL (env-var loading order, late config.update, etc.), we'd
        # destroy the production database on test teardown — which has
        # already happened twice. Fail loudly instead.
        engine_url = str(db.engine.url).lower()
        if "sqlite" not in engine_url or ":memory:" not in engine_url:
            raise RuntimeError(
                f"Test engine bound to {engine_url!r}; refusing to run. "
                "Set DATABASE_URL=sqlite:///:memory: in the environment "
                "before pytest is invoked."
            )
        db.create_all()
        yield flask_app
        db.drop_all()


@pytest.fixture(autouse=True)
def _reset_flask_g(app):
    """Reset Flask's g after each test to prevent Flask-Login user from bleeding via session-scoped app context."""
    yield
    try:
        from flask.globals import _cv_app

        ctx = _cv_app.get(None)
        if ctx is not None:
            ctx.g = app.app_ctx_globals_class()
    except Exception:
        pass


# Sites that did `from app.utils.mailer import send_email_via_brevo_api`
# at module load — the import copies the function reference into their
# namespace, so patching only `app.utils.mailer.send_email_via_brevo_api`
# wouldn't reach them. Every alias is patched here.
_MAILER_ALIASES = (
    "app.utils.mailer.send_email_via_brevo_api",
    "app.tasks.send_email",
    "app.routes.auth.send_email_via_brevo_api",
    "app.routes.user.send_email_via_brevo_api",
    "app.routes.user_tickets.send_email_via_brevo_api",
    "app.routes.user_2fa.send_email_via_brevo_api",
    "app.routes.user_tools.send_email_via_brevo_api",
    "app.routes.admin.manage_users.send_email_via_brevo_api",
    "app.routes.admin.admin.send_email_via_brevo_api",
    "app.cron.remind_unconfirmed_users.send_email_via_brevo_api",
    "app.cron.send_recap_digests.send_email_via_brevo_api",
)


@pytest.fixture(autouse=True)
def _flush_idempotency_locks():
    """Wipe broadcast / reminder Redis locks before every test.

    The locks are real Redis keys with hour-long TTLs, so a previous test
    run that landed a SETNX would block the same payload from re-enqueueing
    for the rest of the suite. CI doesn't have Redis at all, so this fails
    open and the tests rely on the fail-open path inside the route.
    """
    try:
        from redis import Redis

        r = Redis.from_url("redis://localhost:6379/0", socket_connect_timeout=1)
        for pat in ("broadcast:lock:*", "reminder:*"):
            for key in r.scan_iter(pat):
                r.delete(key)
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def mock_email(request, monkeypatch):
    """Auto-mock the Brevo sender across every test.

    Defence in depth: even tests that don't explicitly patch the mailer
    can never reach Brevo. Tests that need to assert on email arguments
    can request this fixture by name and inspect `mock.call_args_list`.

    Tests that test the mailer ITSELF (e.g. dry-run logic, attachment
    handling) opt out via @pytest.mark.no_mock_email so they can drive
    the real send_email_via_brevo_api implementation under their own
    requests.post stubs.
    """
    if request.node.get_closest_marker("no_mock_email"):
        return None
    from unittest.mock import MagicMock

    mock = MagicMock(return_value=True, name="send_email_via_brevo_api")
    for path in _MAILER_ALIASES:
        # raising=False because some aliases might not yet be imported
        # in a minimal test (e.g. cron-only test files); we don't want
        # the fixture to crash the whole suite over an unloaded module.
        monkeypatch.setattr(path, mock, raising=False)
    return mock


@pytest.fixture()
def client(app):
    with app.test_client() as c:
        yield c


@pytest.fixture()
def db(app):
    from app.ext import db as _db

    yield _db
    _db.session.rollback()


# ── Utilisateurs réutilisables ────────────────────────────────────────────────


@pytest.fixture()
def admin_user(app, db):
    from app.models.user import User

    user = User(
        firstname="Admin",
        lastname="Test",
        email="admin@example.com",
        is_admin=True,
        is_confirmed=True,
        abonnement_type="gratuit",
    )
    user.set_password("Admin1234!")
    db.session.add(user)
    db.session.commit()
    yield user
    db.session.delete(user)
    db.session.commit()


@pytest.fixture()
def confirmed_user(app, db):
    from app.models.user import User
    from app.utils.crypto import encrypt_token

    user = User(
        firstname="Alice",
        lastname="Médecin",
        email="alice@example.com",
        is_admin=False,
        is_confirmed=True,
        abonnement_type="gratuit",
        twofa_method="email",  # 2FA is mandatory
        # A usable Free account brings its own AI key — without one the user
        # surface is gated (Free = BYOK, no key = no access). This fixture
        # represents a *configured* Free user so view tests aren't redirected.
        ai_provider="mistral",
        ai_api_key_enc=encrypt_token("sk-test-fixture"),
    )
    user.set_password("Alice1234!")
    db.session.add(user)
    db.session.commit()
    yield user
    db.session.delete(user)
    db.session.commit()


@pytest.fixture()
def subscriber_user(app, db):
    from app.models.user import User

    user = User(
        firstname="Alice",
        lastname="Abonnée",
        email="subscriber@example.com",
        is_admin=False,
        is_confirmed=True,
        abonnement_type="premium_pro",
        twofa_method="email",  # 2FA is mandatory
    )
    user.set_password("Alice1234!")
    db.session.add(user)
    db.session.commit()
    yield user
    db.session.delete(user)
    db.session.commit()


@pytest.fixture()
def unconfirmed_user(app, db):
    from app.models.user import User

    user = User(
        firstname="Bob",
        lastname="Inconnu",
        email="bob@example.com",
        is_admin=False,
        is_confirmed=False,
        abonnement_type="gratuit",
    )
    user.set_password("Bob12345!")
    db.session.add(user)
    db.session.commit()
    yield user
    db.session.delete(user)
    db.session.commit()


@pytest.fixture()
def user_totp(app, db):
    """Confirmed subscriber with TOTP 2FA enabled."""
    import pyotp

    from app.models.user import User

    user = User(
        firstname="Totp",
        lastname="User",
        email="totp@example.com",
        is_admin=False,
        is_confirmed=True,
        abonnement_type="premium_pro",
        twofa_method="totp",
        totp_secret=pyotp.random_base32(),
    )
    user.set_password("Totp1234!")
    db.session.add(user)
    db.session.commit()
    yield user
    db.session.delete(user)
    db.session.commit()


@pytest.fixture()
def user_email_2fa(app, db):
    """Confirmed subscriber with email OTP 2FA enabled."""
    from app.models.user import User

    user = User(
        firstname="Email",
        lastname="Otp",
        email="emailotp@example.com",
        is_admin=False,
        is_confirmed=True,
        abonnement_type="premium_pro",
        twofa_method="email",
    )
    user.set_password("Email123!")
    db.session.add(user)
    db.session.commit()
    yield user
    db.session.delete(user)
    db.session.commit()


@pytest.fixture()
def subscriber_no_2fa(app, db):
    """Confirmed subscriber WITHOUT 2FA — used to test mandatory 2FA enforcement."""
    from app.models.user import User

    user = User(
        firstname="No2fa",
        lastname="User",
        email="no2fa@example.com",
        is_admin=False,
        is_confirmed=True,
        abonnement_type="premium_pro",
        twofa_method=None,
    )
    user.set_password("No2fa123!")
    db.session.add(user)
    db.session.commit()
    yield user
    db.session.delete(user)
    db.session.commit()
