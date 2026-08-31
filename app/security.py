import json
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from app.settings import Settings

PASSWORD_HASHER = PasswordHasher()
JWT_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return PASSWORD_HASHER.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(stored_hash, password)
    except (InvalidHashError, VerifyMismatchError):
        return False


def issue_access_token(user_id: str, settings: Settings) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {"sub": user_id, "iat": now, "exp": now + timedelta(hours=8)},
        settings.jwt_secret.get_secret_value(),
        algorithm=JWT_ALGORITHM,
    )


def read_access_token(token: str, settings: Settings) -> str:
    payload = jwt.decode(token, settings.jwt_secret.get_secret_value(), algorithms=[JWT_ALGORITHM])
    user_id = payload.get("sub")
    if not isinstance(user_id, str):
        raise jwt.InvalidTokenError("missing subject")
    return user_id


def encrypt_credentials(data: dict[str, str], settings: Settings) -> str:
    return (
        Fernet(settings.encryption_key.get_secret_value().encode())
        .encrypt(json.dumps(data, separators=(",", ":")).encode())
        .decode()
    )


def decrypt_credentials(token: str | None, settings: Settings) -> dict[str, str]:
    if not token:
        return {}
    try:
        raw = Fernet(settings.encryption_key.get_secret_value().encode()).decrypt(token.encode())
        value = json.loads(raw)
    except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("stored credentials cannot be decrypted") from error
    if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
        raise ValueError("stored credentials have an invalid shape")
    return value

