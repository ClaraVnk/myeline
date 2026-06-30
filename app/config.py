import os


class Config:
    """Configuration class for the Flask application."""

    _secret_key = os.environ.get("SECRET_KEY")
    if not _secret_key:
        raise ValueError(
            "SECRET_KEY environment variable must be set. Add it to your .env file."
        )
    SECRET_KEY = _secret_key
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL") or "sqlite:///veille.db"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ECHO = os.environ.get("SQLALCHEMY_ECHO", "false").lower() == "true"
    SQLALCHEMY_RECORD_QUERIES = (
        os.environ.get("SQLALCHEMY_RECORD_QUERIES", "false").lower() == "true"
    )

    # MariaDB/MySQL connection pool tuning. Must be a class attribute (not a
    # @property) so Flask's config.from_object() copies the resolved dict —
    # @property objects are copied as descriptors, not evaluated, which silently
    # disables pool_pre_ping and produces "MySQL server has gone away" errors
    # after idle periods.
    SQLALCHEMY_ENGINE_OPTIONS = (
        {
            "pool_pre_ping": True,  # SELECT 1 before each checkout — invalidates dead conns
            "pool_recycle": 280,  # < typical MySQL/proxy idle timeout (5 min)
            "pool_timeout": 20,
            "max_overflow": 10,
            "connect_args": {"connect_timeout": 5},
        }
        if (os.environ.get("DATABASE_URL") or "").startswith(
            ("mysql://", "mysql+pymysql://")
        )
        else {}
    )

    # Flask settings
    FLASK_ENV = os.environ.get("FLASK_ENV", "production")
    _is_prod = FLASK_ENV == "production"
    FLASK_DEBUG = (
        os.environ.get("FLASK_DEBUG", "false" if _is_prod else "true").lower() == "true"
    )
    TESTING = os.environ.get("TESTING", "false").lower() == "true"

    # Security — valeurs par défaut pilotées par FLASK_ENV
    SESSION_COOKIE_SECURE = (
        os.environ.get("SESSION_COOKIE_SECURE", "true" if _is_prod else "false").lower()
        == "true"
    )
    SESSION_COOKIE_HTTPONLY = (
        os.environ.get("SESSION_COOKIE_HTTPONLY", "true").lower() == "true"
    )
    SESSION_COOKIE_SAMESITE = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax")
    PERMANENT_SESSION_LIFETIME = int(
        os.environ.get("PERMANENT_SESSION_LIFETIME", "3600")
    )
    REMEMBER_COOKIE_SECURE = _is_prod
    # Flask-Login defaults to HttpOnly=True but no SameSite — set both
    # explicitly so the remember cookie matches the session cookie's
    # protections (audit H-7).
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = os.environ.get("REMEMBER_COOKIE_SAMESITE", "Lax")

    # CSRF Protection
    WTF_CSRF_ENABLED = os.environ.get("WTF_CSRF_ENABLED", "true").lower() == "true"
    WTF_CSRF_TIME_LIMIT = int(os.environ.get("WTF_CSRF_TIME_LIMIT", "3600"))

    # File uploads
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", "16777216"))  # 16MB
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "uploads")
    ALLOWED_EXTENSIONS = os.environ.get(
        "ALLOWED_EXTENSIONS", "pdf,png,jpg,jpeg,gif"
    ).split(",")

    # Logging
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
    LOG_TO_STDOUT = os.environ.get("LOG_TO_STDOUT", "false").lower() == "true"

    # Redis
    REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

    # Rate limiting — Flask-Limiter lit REDIS_URL directement via flask_limiter.py
    RATELIMIT_DEFAULT = os.environ.get("RATELIMIT_DEFAULT", "200/hour")

    # Monitoring
    HEALTH_CHECK_ENDPOINT = os.environ.get("HEALTH_CHECK_ENDPOINT", "/health")
    METRICS_ENABLED = os.environ.get("METRICS_ENABLED", "false").lower() == "true"

    # Billing (Stripe) is part of the hosted edition only — the open-source
    # self-host build is fully unlocked, no payment layer.

    # Support notifications
    SUPPORT_NOTIFY_EMAIL = os.environ.get("SUPPORT_NOTIFY_EMAIL", "")

    # reCAPTCHA
    RECAPTCHA_SITE_KEY = os.environ.get("RECAPTCHA_SITE_KEY")
    RECAPTCHA_SECRET_KEY = os.environ.get("RECAPTCHA_SECRET_KEY")

    # Google Drive — service account (JSON key content as a single env var)
    GOOGLE_SERVICE_ACCOUNT_CREDENTIALS = os.environ.get(
        "GOOGLE_SERVICE_ACCOUNT_CREDENTIALS", ""
    )

    # Base URL for OAuth redirect URIs — override to force localhost in dev
    OAUTH_REDIRECT_BASE_URL = os.environ.get("OAUTH_REDIRECT_BASE_URL", "")

    # Social login (Google, Microsoft, Apple) — separate credentials from Drive
    GOOGLE_LOGIN_CLIENT_ID = os.environ.get("GOOGLE_LOGIN_CLIENT_ID")
    GOOGLE_LOGIN_CLIENT_SECRET = os.environ.get("GOOGLE_LOGIN_CLIENT_SECRET")
    MICROSOFT_LOGIN_CLIENT_ID = os.environ.get("MICROSOFT_LOGIN_CLIENT_ID")
    MICROSOFT_LOGIN_CLIENT_SECRET = os.environ.get("MICROSOFT_LOGIN_CLIENT_SECRET")
    APPLE_LOGIN_CLIENT_ID = os.environ.get("APPLE_LOGIN_CLIENT_ID")  # Service ID
    APPLE_LOGIN_TEAM_ID = os.environ.get("APPLE_LOGIN_TEAM_ID")
    APPLE_LOGIN_KEY_ID = os.environ.get("APPLE_LOGIN_KEY_ID")
    APPLE_LOGIN_PRIVATE_KEY = os.environ.get("APPLE_LOGIN_PRIVATE_KEY", "").replace(
        "\\n", "\n"
    )

    # OneDrive OAuth (Microsoft Azure app)
    ONEDRIVE_CLIENT_ID = os.environ.get("ONEDRIVE_CLIENT_ID")
    ONEDRIVE_CLIENT_SECRET = os.environ.get("ONEDRIVE_CLIENT_SECRET")

    # Dropbox OAuth
    DROPBOX_APP_KEY = os.environ.get("DROPBOX_APP_KEY")
    DROPBOX_APP_SECRET = os.environ.get("DROPBOX_APP_SECRET")

    # kDrive (Infomaniak) OAuth
    KDRIVE_CLIENT_ID = os.environ.get("KDRIVE_CLIENT_ID")
    KDRIVE_CLIENT_SECRET = os.environ.get("KDRIVE_CLIENT_SECRET")

    # Mistral API — platform default LLM for RAG synthesis, HyDE, reranking, query
    # variants, and contextual retrieval. French (EU-hosted), GDPR-aligned.
    # When set, replaces all Ollama LLM calls; embedding stays local (bge-m3).
    # Synthesis uses MISTRAL_LLM_MODEL (medium); auxiliary calls use MISTRAL_AUX_MODEL (small).
    # OpenAI / Anthropic / Google Gemini are available only as opt-in per-org alternatives
    # for Enterprise organisations — each org brings its own API key via its admin panel.
    MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
    MISTRAL_LLM_MODEL = os.environ.get("MISTRAL_LLM_MODEL", "mistral-medium-latest")
    MISTRAL_AUX_MODEL = os.environ.get("MISTRAL_AUX_MODEL", "mistral-small-latest")

    # Deployment mode — set by scripts/install.sh. One of:
    #   classic   — SaaS multi-tenant (myeline.io style + resellers)
    #   sovereign — air-gap on-prem (no external API call ever)
    #   hybrid    — sovereign-hybride: on-prem + BYOK external APIs
    # Drives feature gating across the whole app — see
    # app/utils/deployment_mode.py for the predicates.
    DEPLOYMENT_MODE = os.environ.get("DEPLOYMENT_MODE", "classic").strip().lower()

    # Sovereign mode — back-compat alias. Hard guarantee that NO external AI
    # provider call is ever issued, even if API keys are configured. All
    # synthesis routes through the local Ollama instance. Used for on-premise
    # deployments in regulated industries (defence, health, legal, finance)
    # and as a UI signal to hide the per-org provider picker.
    # Set to True automatically when DEPLOYMENT_MODE=sovereign.
    SOVEREIGN_MODE = (
        os.environ.get("SOVEREIGN_MODE", "false").lower() in ("true", "1", "yes")
        or os.environ.get("DEPLOYMENT_MODE", "").strip().lower() == "sovereign"
    )

    # Public-facing base URL of this deployment, used to build absolute links in
    # transactional / digest emails (logo src, dashboard CTA…). Required because
    # cron-sent emails have no Flask request context to derive host_url from.
    # Default `https://myeline.io` keeps the cloud install working out of the box;
    # on-premise deployments override with their internal URL.
    EXTERNAL_URL = os.environ.get("EXTERNAL_URL", "http://localhost:5000").rstrip("/")

    LOGS_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)), "..", "logs")
    REPORTS_DIR = os.path.join(
        os.path.abspath(os.path.dirname(__file__)), "..", "data/raw"
    )

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    @classmethod
    def validate_production(cls):
        """Fail fast on missing critical secrets when FLASK_ENV=production."""
        import warnings

        required = {
            # Fernet master key for every encrypted DB column (TOTP secrets,
            # OAuth/refresh tokens, BYOK API keys). Without it the app boots and
            # only crashes on the first encrypted read/write — fail fast instead.
            "CLOUD_TOKEN_KEY": os.environ.get("CLOUD_TOKEN_KEY"),
            # Transactional email (account confirmation, 2FA codes, digests).
            "BREVO_API_KEY": os.environ.get("BREVO_API_KEY"),
        }

        missing = [k for k, v in required.items() if not v]
        if missing:
            msg = f"Variables manquantes en production : {', '.join(missing)}"
            if cls.FLASK_ENV == "production":
                raise ValueError(msg)
            warnings.warn(msg, stacklevel=3)

        # FILE_ENCRYPTION_KEY is only exercised by encrypted file uploads, which
        # not every deployment enables — warn rather than hard-fail.
        if cls.FLASK_ENV == "production" and not os.environ.get("FILE_ENCRYPTION_KEY"):
            warnings.warn(
                "FILE_ENCRYPTION_KEY not set — encrypted file uploads will fail at runtime.",
                stacklevel=3,
            )
