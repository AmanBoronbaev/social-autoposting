import re
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", env_prefix="APP_", extra="ignore")

    database_url: str = "postgresql+psycopg://autoposting:autoposting@postgres:5432/autoposting"
    public_base_url: AnyHttpUrl = "https://post.example.com"
    jwt_secret: SecretStr
    encryption_key: SecretStr
    media_dir: Path = Path("/var/lib/autoposting/media")
    max_upload_bytes: int = Field(default=52_428_800, ge=1, le=5 * 1024**3)
    worker_poll_seconds: int = Field(default=3, ge=1, le=60)
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: SecretStr | None = None
    admin_path: str = Field(default="/", validation_alias="APP_PATH")

    zernio_api_base_url: AnyHttpUrl = Field(default="https://zernio.com/api/v1", validation_alias="ZERNIO_API_BASE_URL")

    telegram_api_base_url: AnyHttpUrl = Field(
        default="https://api.telegram.org", validation_alias="TELEGRAM_API_BASE_URL"
    )
    telegram_max_upload_bytes: int = Field(
        default=52_428_800,
        ge=1,
        le=2_000 * 1024**2,
        validation_alias="TELEGRAM_MAX_UPLOAD_BYTES",
    )
    whapi_max_media_bytes: int = Field(
        default=100 * 1024**2,
        ge=1,
        le=2 * 1024**3,
        validation_alias="WHAPI_MAX_MEDIA_BYTES",
    )
    media_prepare_timeout_seconds: int = Field(default=900, ge=30, le=7200)

    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("APP_JWT_SECRET must be at least 32 characters")
        return value

    @field_validator("encryption_key")
    @classmethod
    def validate_encryption_key(cls, value: SecretStr) -> SecretStr:
        try:
            Fernet(value.get_secret_value().encode())
        except (TypeError, ValueError) as error:
            raise ValueError("APP_ENCRYPTION_KEY must be a Fernet key") from error
        return value

    @field_validator("admin_path")
    @classmethod
    def validate_admin_path(cls, value: str) -> str:
        if value == "/":
            return value
        if not re.fullmatch(r"/[A-Za-z0-9._-]{12,128}/", value):
            raise ValueError("APP_PATH must be / or a URL-safe path ending in /")
        return value

    @property
    def public_base(self) -> str:
        return str(self.public_base_url).rstrip("/")

    @property
    def path_prefix(self) -> str:
        return "" if self.admin_path == "/" else self.admin_path.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
