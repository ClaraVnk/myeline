"""Symmetric encryption for tokens (DB) and files (disk)."""

import base64
import hashlib
import logging
import os
from functools import lru_cache
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from sqlalchemy.types import Text, TypeDecorator


logger = logging.getLogger(__name__)

# Static label used as PBKDF2 salt — acceptable for key derivation from a
# high-entropy env var (not password hashing). Changing this value invalidates
# all tokens encrypted after the v2 migration, so treat it as immutable.
_KDF_SALT = b"myeline-token-v2"
_KDF_ITERATIONS = 260_000


@lru_cache(maxsize=4)
def _fernet_from_key(key: str) -> Fernet:
    """Derive a Fernet from a raw key string (PBKDF2-SHA256, 260k iterations).

    Result is cached: PBKDF2 with 260k iterations costs 30-80 ms per call,
    and `EncryptedString` columns hit this path on EVERY column read/write
    (sometimes hundreds of times per request — see RagFeedback admin
    dashboard, /recherche/history, watch-alerts cron). Caching the derived
    Fernet means we pay the KDF once per process per distinct key.
    The cache is bounded to 4 entries (covers CLOUD_TOKEN_KEY +
    FILE_ENCRYPTION_KEY + key rotation transient state).
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_KDF_SALT,
        iterations=_KDF_ITERATIONS,
    )
    return Fernet(base64.urlsafe_b64encode(kdf.derive(key.encode())))


def _fernet(env_key: str = "CLOUD_TOKEN_KEY") -> Fernet:
    """Derive a Fernet key from env_key using PBKDF2-SHA256.

    Requires CLOUD_TOKEN_KEY (or the specified env_key) to be set explicitly —
    no fallback to SECRET_KEY to prevent conflating session signing with
    data encryption.

    The KDF result is cached via _fernet_from_key, so this whole path is
    a dict lookup once warm.
    """
    key = os.environ.get(env_key)
    if not key:
        raise RuntimeError(
            f"{env_key} is not configured. "
            "Set it to a strong random secret (e.g. `openssl rand -base64 32`)."
        )
    return _fernet_from_key(key)


@lru_cache(maxsize=2)
def _fernet_legacy_cached(key: str) -> Fernet:
    """Cached legacy fallback (raw SHA-256). See _fernet_from_key for rationale."""
    raw = hashlib.sha256(key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def _fernet_legacy(env_key: str = "CLOUD_TOKEN_KEY") -> Fernet:
    """Legacy KDF (raw SHA-256, no iterations) — used only as decrypt fallback.

    Tokens encrypted before the PBKDF2 migration are still readable via this
    path. New tokens are always written with _fernet() (PBKDF2).
    """
    key = (
        os.environ.get(env_key)
        or os.environ.get("CLOUD_TOKEN_KEY")
        or os.environ.get("SECRET_KEY", "")
    )
    return _fernet_legacy_cached(key)


def _file_fernet() -> Fernet:
    return _fernet("FILE_ENCRYPTION_KEY")


# ── Token encryption (OAuth, API keys stored in DB) ──────────────────────────


def encrypt_token(plaintext: str, raw_key: Optional[str] = None) -> str:
    """Encrypt with the env-configured CLOUD_TOKEN_KEY, or a caller-supplied
    raw key (used by the rotation CLI to write under the new key)."""
    fernet = _fernet_from_key(raw_key) if raw_key else _fernet()
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str, raw_key: Optional[str] = None) -> str:
    """Decrypt with the env-configured CLOUD_TOKEN_KEY (or caller-supplied raw
    key — used by the rotation CLI to read under the old key). Falls back to
    the legacy raw-SHA-256 KDF for tokens written before the PBKDF2 migration."""
    fernet = _fernet_from_key(raw_key) if raw_key else _fernet()
    try:
        return fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        # Fallback: token was encrypted before the PBKDF2 migration.
        try:
            plaintext = _fernet_legacy().decrypt(ciphertext.encode()).decode()
            logger.warning(
                "Credential decrypted with legacy KDF — re-save it to upgrade encryption"
            )
            return plaintext
        except InvalidToken as e:
            raise ValueError(
                "Token decryption failed — wrong key or corrupted data"
            ) from e


# ── File encryption (user documents stored on disk) ──────────────────────────


def encrypt_file(path) -> None:
    """Encrypt a file in-place using FILE_ENCRYPTION_KEY."""
    from pathlib import Path

    p = Path(path)
    p.write_bytes(_file_fernet().encrypt(p.read_bytes()))


def decrypt_file_to_temp(path):
    """
    Decrypt an encrypted file into a NamedTemporaryFile.
    Caller must close/delete the temp file after use.
    The temp file preserves the original file suffix for MIME detection.
    """
    import tempfile
    from pathlib import Path

    p = Path(path)
    try:
        plaintext = _file_fernet().decrypt(p.read_bytes())
    except InvalidToken as e:
        raise ValueError(f"File decryption failed for {p.name}") from e
    suffix = p.suffix
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(plaintext)
    tmp.flush()
    tmp.close()
    return tmp


# ── Transparent column encryption (SQLAlchemy TypeDecorator) ─────────────────


class EncryptedString(TypeDecorator):
    """SQLAlchemy column type that transparently encrypts/decrypts strings via Fernet.

    Use for sensitive fields stored in the DB (TOTP secrets, query history,
    watch alert keywords, RAG feedback content). Legacy plaintext rows
    (pre-migration) are returned as-is so the data migration can happen lazily;
    but a value that looks like ciphertext yet fails to decrypt raises (wrong
    key / corruption) instead of leaking the raw token. Writes always produce
    ciphertext.

    Storage format: Fernet token (base64). Plaintext expansion is ~1.4× +
    headers (~57 bytes overhead), so the underlying column is Text with no
    length cap.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or value == "":
            return value
        if not isinstance(value, str):
            value = str(value)
        return encrypt_token(value)

    def process_result_value(self, value, dialect):
        if value is None or value == "":
            return value
        try:
            return decrypt_token(value)
        except (ValueError, InvalidToken):
            # A Fernet token always starts with "gAAAAA" (version byte 0x80 +
            # a zero-padded timestamp, base64url-encoded). If a value that
            # *looks* like ciphertext fails to decrypt, the key is wrong or the
            # row is corrupt — surface it loudly rather than handing back the
            # raw ciphertext as if it were the plaintext secret.
            if isinstance(value, str) and value.startswith("gAAAAA"):
                logger.error(
                    "Failed to decrypt an EncryptedString column — wrong "
                    "CLOUD_TOKEN_KEY or corrupted data. Refusing to return "
                    "ciphertext as plaintext."
                )
                raise ValueError(
                    "EncryptedString decryption failed — wrong key or corrupted data"
                )
            # Otherwise it's a legacy plaintext row (pre-migration) — return
            # verbatim; the next write will encrypt it.
            return value
