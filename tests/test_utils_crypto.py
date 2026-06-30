"""Tests for app/utils/crypto.py — encrypt_token / decrypt_token."""
import os

import pytest


class TestEncryptToken:
    def test_encrypt_returns_string(self):
        from app.utils.crypto import encrypt_token
        result = encrypt_token("hello")
        assert isinstance(result, str)

    def test_encrypt_is_not_plaintext(self):
        from app.utils.crypto import encrypt_token
        result = encrypt_token("my_secret_token")
        assert "my_secret_token" not in result

    def test_encrypt_different_calls_produce_different_ciphertext(self):
        """Fernet uses a random IV, so two encryptions of the same plaintext differ."""
        from app.utils.crypto import encrypt_token
        c1 = encrypt_token("same")
        c2 = encrypt_token("same")
        assert c1 != c2

    def test_encrypt_empty_string(self):
        from app.utils.crypto import encrypt_token
        result = encrypt_token("")
        assert isinstance(result, str)
        assert len(result) > 0


class TestDecryptToken:
    def test_roundtrip(self):
        from app.utils.crypto import decrypt_token, encrypt_token
        plaintext = "oauth_access_token_12345"
        ciphertext = encrypt_token(plaintext)
        assert decrypt_token(ciphertext) == plaintext

    def test_roundtrip_empty_string(self):
        from app.utils.crypto import decrypt_token, encrypt_token
        ciphertext = encrypt_token("")
        assert decrypt_token(ciphertext) == ""

    def test_roundtrip_unicode(self):
        from app.utils.crypto import decrypt_token, encrypt_token
        plaintext = "token avec des accents éàü et 中文"
        ciphertext = encrypt_token(plaintext)
        assert decrypt_token(ciphertext) == plaintext

    def test_roundtrip_long_token(self):
        from app.utils.crypto import decrypt_token, encrypt_token
        plaintext = "x" * 4096
        ciphertext = encrypt_token(plaintext)
        assert decrypt_token(ciphertext) == plaintext

    def test_decrypt_invalid_ciphertext_raises_value_error(self):
        from app.utils.crypto import decrypt_token
        with pytest.raises(ValueError, match="Token decryption failed"):
            decrypt_token("not-valid-ciphertext")

    def test_decrypt_wrong_key_raises_value_error(self, monkeypatch):
        from app.utils.crypto import encrypt_token
        ciphertext = encrypt_token("secret")
        # Change the key so decryption fails
        monkeypatch.setenv("CLOUD_TOKEN_KEY", "totally-different-key-that-wont-work")
        # Force module to re-read env by calling _fernet() fresh
        from cryptography.fernet import InvalidToken

        from app.utils import crypto as crypto_mod
        with pytest.raises(ValueError, match="Token decryption failed"):
            crypto_mod.decrypt_token(ciphertext)

    def test_decrypt_corrupted_data_raises_value_error(self):
        from app.utils.crypto import decrypt_token, encrypt_token
        ciphertext = encrypt_token("valid")
        # Corrupt a byte in the middle
        corrupted = ciphertext[:-4] + "XXXX"
        with pytest.raises(ValueError, match="Token decryption failed"):
            decrypt_token(corrupted)


class TestFernetKeyDerivation:
    def test_uses_cloud_token_key_env_var(self, monkeypatch):
        """CLOUD_TOKEN_KEY takes priority over SECRET_KEY."""
        monkeypatch.setenv("CLOUD_TOKEN_KEY", "my-cloud-key")
        from app.utils.crypto import decrypt_token, encrypt_token
        ct = encrypt_token("hello")
        assert decrypt_token(ct) == "hello"

    def test_missing_cloud_token_key_raises_runtime_error(self, monkeypatch):
        """CLOUD_TOKEN_KEY must be set explicitly — no fallback to SECRET_KEY."""
        monkeypatch.delenv("CLOUD_TOKEN_KEY", raising=False)
        from app.utils import crypto as crypto_mod
        with pytest.raises(RuntimeError, match="CLOUD_TOKEN_KEY"):
            crypto_mod.encrypt_token("fail")

    def test_legacy_token_still_decrypts(self, monkeypatch):
        """Tokens encrypted with the old SHA-256 KDF remain readable via fallback."""
        import base64
        import hashlib

        from cryptography.fernet import Fernet

        key = os.environ.get("CLOUD_TOKEN_KEY", "test-secret")
        raw = hashlib.sha256(key.encode()).digest()
        legacy_fernet = Fernet(base64.urlsafe_b64encode(raw))
        legacy_ct = legacy_fernet.encrypt(b"legacy-value").decode()

        from app.utils import crypto as crypto_mod
        assert crypto_mod.decrypt_token(legacy_ct) == "legacy-value"
