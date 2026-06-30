"""Tests for utility functions — tokens and user_services."""
import pytest


class TestIsStrongPassword:
    def test_valid_password(self, app):
        from app.services.user_services import is_strong_password
        with app.app_context():
            assert is_strong_password("Str0ng!pw") is True

    def test_empty_returns_false(self, app):
        from app.services.user_services import is_strong_password
        with app.app_context():
            assert is_strong_password("") is False

    def test_none_returns_false(self, app):
        from app.services.user_services import is_strong_password
        with app.app_context():
            assert is_strong_password(None) is False

    def test_no_uppercase_fails(self, app):
        from app.services.user_services import is_strong_password
        with app.app_context():
            assert is_strong_password("str0ng!pw") is False

    def test_no_lowercase_fails(self, app):
        from app.services.user_services import is_strong_password
        with app.app_context():
            assert is_strong_password("STR0NG!PW") is False

    def test_no_digit_fails(self, app):
        from app.services.user_services import is_strong_password
        with app.app_context():
            assert is_strong_password("Strong!pw") is False

    def test_no_special_fails(self, app):
        from app.services.user_services import is_strong_password
        with app.app_context():
            assert is_strong_password("Str0ngpwd") is False

    def test_too_short_fails(self, app):
        from app.services.user_services import is_strong_password
        with app.app_context():
            assert is_strong_password("S1!a") is False


class TestTokens:
    def test_generate_and_confirm_token(self, app):
        from app.utils.tokens import confirm_token, generate_confirmation_token
        with app.app_context():
            token = generate_confirmation_token("test@example.com")
            email = confirm_token(token)
            assert email == "test@example.com"

    def test_invalid_token_returns_none(self, app):
        from app.utils.tokens import confirm_token
        with app.app_context():
            result = confirm_token("this-is-not-a-valid-token")
            assert result is None

    def test_bad_signature_returns_none(self, app):
        from app.utils.tokens import confirm_token
        with app.app_context():
            result = confirm_token("bad.signature.here")
            assert result is None


class TestGenerateUnsubscribeToken:
    def test_generates_token(self, app, confirmed_user):
        from app.services.user_services import generate_unsubscribe_token
        with app.app_context():
            token = generate_unsubscribe_token(confirmed_user)
            assert token is not None
            assert isinstance(token, str)
            assert len(token) > 10
