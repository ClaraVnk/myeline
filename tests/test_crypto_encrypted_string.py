"""Tests for EncryptedString TypeDecorator."""

import os

import pytest


# CLOUD_TOKEN_KEY must be set for Fernet derivation. conftest.py sets the
# other essentials; provide a stable value here so the test is deterministic.
os.environ.setdefault("CLOUD_TOKEN_KEY", "ci-test-cloud-token-key")


@pytest.fixture(scope="module")
def es():
    from app.utils.crypto import EncryptedString
    return EncryptedString()


def test_round_trip_basic(es):
    ct = es.process_bind_param("plain value", None)
    assert ct.startswith("gAAAA"), "Fernet ciphertext should be base64-encoded with version 0x80"
    assert es.process_result_value(ct, None) == "plain value"


def test_round_trip_unicode(es):
    plaintext = "héllo wörld 🇫🇷 — accents & emoji"
    ct = es.process_bind_param(plaintext, None)
    assert es.process_result_value(ct, None) == plaintext


def test_legacy_plaintext_fallback(es):
    """Pre-migration rows stored in plaintext should be returned verbatim."""
    legacy = "this-is-not-a-fernet-token"
    assert es.process_result_value(legacy, None) == legacy


def test_null_and_empty_handling(es):
    assert es.process_bind_param(None, None) is None
    assert es.process_bind_param("", None) == ""
    assert es.process_result_value(None, None) is None
    assert es.process_result_value("", None) == ""


def test_non_string_value_coerced(es):
    """Defensive: numeric values (e.g. shouldn't happen but just in case) get str()'d."""
    ct = es.process_bind_param(12345, None)
    assert es.process_result_value(ct, None) == "12345"


def test_each_encryption_uses_fresh_iv(es):
    """Fernet uses a random IV per encryption, so two writes of the same plaintext
    must produce different ciphertexts (defends against ciphertext correlation)."""
    a = es.process_bind_param("same value", None)
    b = es.process_bind_param("same value", None)
    assert a != b
    assert es.process_result_value(a, None) == es.process_result_value(b, None) == "same value"
