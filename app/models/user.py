from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from flask_login import UserMixin
from werkzeug.security import check_password_hash

from app.ext import db
from app.utils.crypto import EncryptedString
from app.utils.time_utils import utcnow


# Argon2id is the OWASP-2024 recommended KDF for passwords (audit H-6).
# Werkzeug's `generate_password_hash` defaults vary across versions
# (PBKDF2-SHA256 in 2.x, scrypt in 3.x), so a rebuild can silently change
# the algorithm — non-deterministic security posture. Pinning argon2id
# explicitly gives us memory-hard KDF + known parameters.
#
# Parameters are OWASP 2024 minimum for argon2id:
#   memory_cost = 19 MiB, time_cost = 2, parallelism = 1
# Hash output starts with "$argon2id$..." which check_password() detects
# below; legacy PBKDF2/scrypt hashes (starting with "pbkdf2:" / "scrypt:")
# remain readable so existing users keep working until next login.
_PWD_HASHER = PasswordHasher(memory_cost=19 * 1024, time_cost=2, parallelism=1)

# Precomputed argon2id hash used to equalise login response time when the
# email doesn't exist: the real path runs an argon2 verify only when the user
# is found, so a missing account would otherwise be distinguishable by timing.
_DUMMY_PWD_HASH = _PWD_HASHER.hash("timing-equalisation-placeholder")


def dummy_password_check(password: str = "x") -> None:
    """Burn roughly one argon2 verify so the no-such-user login path costs the
    same as a wrong-password one. Always fails; the result is discarded."""
    try:
        _PWD_HASHER.verify(_DUMMY_PWD_HASH, password)
    except Exception:
        pass


class User(UserMixin, db.Model):  # type: ignore
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False)
    firstname = db.Column(
        db.String(100),
        nullable=True,
    )
    lastname = db.Column(
        db.String(100),
        nullable=True,
    )
    password_hash = db.Column(db.String(255), nullable=False)
    # Flag levé quand un password est créé par un tiers (provision-init
    # tenant Enterprise, reset admin par un opérateur, etc.) — l'auth
    # middleware redirige vers /auth/reset-password tant qu'il est True,
    # bloquant tout accès à l'app au-delà du formulaire de reset.
    # Reset à False par le flow de changement de password.
    must_reset_password = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )
    is_admin = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True, index=True)
    abonnement_type = db.Column(db.String(50), default="gratuit")
    is_confirmed = db.Column(db.Boolean, default=False, index=True)
    confirmation_token = db.Column(db.String(128), nullable=True)
    last_login = db.Column(db.DateTime, default=None, index=True)
    last_digest_sent_at = db.Column(db.DateTime, nullable=True)
    newsletter_frequency = db.Column(
        db.String(10), nullable=False, default="none", server_default="none"
    )

    exclude_from_stats = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )

    # Per-user BYOK (bring-your-own-key) for the chat/synthesis LLM.
    # Set these to use your own provider key for AI synthesis instead of
    # the included platform default.
    ai_provider = db.Column(
        db.String(20), nullable=True
    )  # "mistral" | "openai" | "gemini" | "claude" | None
    ai_api_key_enc = db.Column(db.Text, nullable=True)  # Fernet-encrypted API key
    ai_model = db.Column(db.String(100), nullable=True)  # optional model override
    # Per-user BYOK for the EMBEDDING provider (separate from synthesis: Claude/
    # Gemini have no embedding API used here). On Free, the user brings this key
    # so personal indexing costs the platform €0; Pro keeps the included
    # platform Mistral embedding by default. Providers: mistral/openai/voyage/cohere.
    embed_provider = db.Column(db.String(32), nullable=True)
    embed_api_key_enc = db.Column(db.Text, nullable=True)  # Fernet-encrypted API key
    embed_model = db.Column(db.String(64), nullable=True)  # optional model override
    # Opt-in: AI-summarise digest articles with the user's own key.
    digest_summarize = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )

    totp_secret = db.Column(EncryptedString(), nullable=True)
    # twofa_method: None | 'totp' | 'email'
    twofa_method = db.Column(db.String(10), nullable=True)

    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    updated_at = db.Column(
        db.TIMESTAMP,
        server_default=db.func.current_timestamp(),
        server_onupdate=db.func.current_timestamp(),
    )

    def set_password(self, password):
        """Hash and set the user's password.

        New hashes use argon2id with OWASP 2024 parameters. Existing
        users keep their legacy PBKDF2/scrypt hash until they next log
        in — at which point check_password rehashes them on success.
        """
        self.password_hash = _PWD_HASHER.hash(password)

    def check_password(self, password):
        """Verify if the provided password matches the stored hash.

        Reads both argon2id (new) and legacy Werkzeug formats
        (pbkdf2/scrypt) — `argon2.PasswordHasher` only handles its own
        format, so we route by prefix. On a successful legacy match we
        opportunistically rehash to argon2id (rolls users forward
        without forcing a password reset).
        """
        ph = self.password_hash or ""
        if ph.startswith("$argon2"):
            try:
                _PWD_HASHER.verify(ph, password)
            except VerifyMismatchError:
                return False
            except Exception:
                return False
            # Re-hash if argon2 parameters have changed since the hash
            # was computed (e.g. memory_cost bumped) — keeps the cost
            # ratchet moving forward without admin intervention.
            try:
                if _PWD_HASHER.check_needs_rehash(ph):
                    self.password_hash = _PWD_HASHER.hash(password)
            except Exception:
                pass
            return True

        # Legacy: werkzeug's check_password_hash handles pbkdf2/scrypt.
        if check_password_hash(ph, password):
            # Opportunistic upgrade to argon2id on next successful login.
            self.password_hash = _PWD_HASHER.hash(password)
            return True
        return False

    @property
    def is_subscriber(self) -> bool:
        # OSS single-user build: all paid gating is removed. Every account
        # has full access. Name kept so existing `_require_subscriber`
        # gates keep compiling.
        return True

    @property
    def is_pro_subscriber(self) -> bool:
        # OSS single-user build: Pro features are always unlocked.
        return True

    @property
    def has_ai_key(self) -> bool:
        return bool(self.ai_provider and self.ai_api_key_enc)

    @property
    def has_embed_key(self) -> bool:
        return bool(self.embed_provider and self.embed_api_key_enc)

    @property
    def has_2fa(self) -> bool:
        return self.twofa_method is not None

    @property
    def totp_enabled(self) -> bool:
        return self.twofa_method == "totp"

    def get_id(self):
        """Return the user's ID as a string.

        Returns:
            str: User ID as string
        """
        return str(self.id)
