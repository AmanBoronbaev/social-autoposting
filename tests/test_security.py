import jwt

from app.security import (
    decrypt_credentials,
    encrypt_credentials,
    hash_password,
    issue_access_token,
    read_access_token,
    verify_password,
)
from app.settings import get_settings


def test_password_hash_does_not_accept_wrong_password() -> None:
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("wrong password", stored)


def test_encrypted_connection_credential_round_trip() -> None:
    settings = get_settings()
    encrypted = encrypt_credentials({"api_token": "secret-value"}, settings)
    assert "secret-value" not in encrypted
    assert decrypt_credentials(encrypted, settings) == {"api_token": "secret-value"}


def test_access_token_requires_valid_signature() -> None:
    settings = get_settings()
    token = issue_access_token("user-id", settings)
    assert read_access_token(token, settings) == "user-id"
    try:
        jwt.decode(token, "another-test-secret-must-also-be-at-least-thirty-two-bytes", algorithms=["HS256"])
    except jwt.InvalidSignatureError:
        pass
    else:
        raise AssertionError("token unexpectedly accepted by a different key")
