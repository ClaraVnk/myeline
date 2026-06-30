"""Coverage tests for app.utils.crypto — file encryption, missing env, legacy fallback."""
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.utils import crypto


def test_fernet_missing_env_raises(monkeypatch):
    """If env var is unset, _fernet raises a clear RuntimeError."""
    monkeypatch.delenv("CLOUD_TOKEN_KEY", raising=False)
    crypto._fernet_from_key.cache_clear()
    with pytest.raises(RuntimeError, match="CLOUD_TOKEN_KEY is not configured"):
        crypto._fernet()


def test_decrypt_token_legacy_fallback(monkeypatch):
    """A token encrypted with the legacy raw-SHA-256 KDF still decrypts."""
    monkeypatch.setenv("CLOUD_TOKEN_KEY", "rotation-test-key")
    crypto._fernet_from_key.cache_clear()
    crypto._fernet_legacy_cached.cache_clear()
    # Write a token using the legacy KDF, then read with decrypt_token.
    legacy = crypto._fernet_legacy()
    token = legacy.encrypt(b"legacy-secret").decode()
    with patch.object(crypto.logger, "warning") as warn:
        result = crypto.decrypt_token(token)
    assert result == "legacy-secret"
    warn.assert_called_once()


def test_decrypt_token_invalid_token_raises(monkeypatch):
    """Garbage ciphertext fails both PBKDF2 and legacy paths → ValueError."""
    monkeypatch.setenv("CLOUD_TOKEN_KEY", "rotation-test-key-xyz")
    crypto._fernet_from_key.cache_clear()
    crypto._fernet_legacy_cached.cache_clear()
    with pytest.raises(ValueError, match="decryption failed"):
        crypto.decrypt_token("not-a-real-fernet-token")


def test_encrypt_decrypt_with_explicit_raw_key():
    """encrypt_token + decrypt_token round-trip with an explicit raw_key."""
    key = "operator-supplied-key"
    cipher = crypto.encrypt_token("hello", raw_key=key)
    assert crypto.decrypt_token(cipher, raw_key=key) == "hello"


def test_encrypt_file_and_decrypt_file_to_temp(tmp_path, monkeypatch):
    """encrypt_file writes ciphertext in-place; decrypt_file_to_temp restores plaintext."""
    monkeypatch.setenv("FILE_ENCRYPTION_KEY", "file-test-key-zzz")
    crypto._fernet_from_key.cache_clear()
    p: Path = tmp_path / "doc.txt"
    p.write_bytes(b"super-secret-bytes")

    crypto.encrypt_file(p)
    encrypted = p.read_bytes()
    assert b"super-secret-bytes" not in encrypted

    tmp = crypto.decrypt_file_to_temp(p)
    try:
        assert Path(tmp.name).read_bytes() == b"super-secret-bytes"
        assert Path(tmp.name).suffix == ".txt"
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def test_decrypt_file_to_temp_invalid_raises(tmp_path, monkeypatch):
    """A malformed encrypted file raises ValueError with its name."""
    monkeypatch.setenv("FILE_ENCRYPTION_KEY", "file-test-key-aaa")
    crypto._fernet_from_key.cache_clear()
    p = tmp_path / "broken.bin"
    p.write_bytes(b"not encrypted, just bytes")
    with pytest.raises(ValueError, match="decryption failed for broken.bin"):
        crypto.decrypt_file_to_temp(p)
