"""Unit tests for service functions — admin_services, user_services."""
import os
import tempfile

import pytest


# ── admin_services ────────────────────────────────────────────────────────────

class TestReadFileIfSafe:
    def test_reads_valid_file(self, tmp_path):
        from app.services.admin_services import read_file_if_safe
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        result = read_file_if_safe("test.txt", tmp_path)
        assert result == "hello world"

    def test_raises_on_path_traversal(self, tmp_path):
        from app.services.admin_services import read_file_if_safe
        with pytest.raises(PermissionError):
            read_file_if_safe("../../etc/passwd", tmp_path)

    def test_raises_on_missing_file(self, tmp_path):
        from app.services.admin_services import read_file_if_safe
        with pytest.raises(Exception):
            read_file_if_safe("nonexistent.txt", tmp_path)


# ── user_services ─────────────────────────────────────────────────────────────

class TestIsStrongPasswordEdgeCases:
    def test_exactly_8_chars_valid(self, app):
        from app.services.user_services import is_strong_password
        with app.app_context():
            assert is_strong_password("Aa1!bcde") is True

    def test_7_chars_fails(self, app):
        from app.services.user_services import is_strong_password
        with app.app_context():
            assert is_strong_password("Aa1!bcd") is False

    def test_only_alphanumeric_fails(self, app):
        from app.services.user_services import is_strong_password
        with app.app_context():
            assert is_strong_password("Aa12345678") is False


# ── password_utils ────────────────────────────────────────────────────────────

class TestGenerateStrongPassword:
    def test_generates_valid_password(self, app):
        """100 itérations — défonce les bugs de probabilité (ex. un char du
        générateur que le validateur considère invalide tombait 1 fois sur N)."""
        from app.services.user_services import is_strong_password
        from app.utils.password_utils import generate_strong_password
        with app.app_context():
            for _ in range(100):
                pwd = generate_strong_password()
                assert isinstance(pwd, str)
                assert len(pwd) >= 8
                assert is_strong_password(pwd), f"Failed for password: {pwd!r}"

    def test_generates_different_passwords(self, app):
        from app.utils.password_utils import generate_strong_password
        with app.app_context():
            pwd1 = generate_strong_password()
            pwd2 = generate_strong_password()
            assert pwd1 != pwd2
