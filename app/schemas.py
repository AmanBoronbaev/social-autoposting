from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator


class AdminUserIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    display_name: str = Field(default="", max_length=160)
    is_superuser: bool = False


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"


class ConnectionIn(BaseModel):
    provider: Literal["zernio", "telegram", "whapi"]
    platform: Literal["instagram", "tiktok", "telegram", "whatsapp"]
    label: str = Field(min_length=1, max_length=160)
    external_id: str = Field(min_length=1, max_length=255)
    @field_validator("external_id")
    @classmethod
    def normalize_external_id(cls, value: str) -> str:
        return value.strip()


class ProviderCredentialIn(BaseModel):
    provider: Literal["zernio", "telegram", "whapi"]
    api_token: str = Field(min_length=10, max_length=4096)


class TikTokSettingsIn(BaseModel):
    privacy_level: str = Field(min_length=1, max_length=64)
    allow_comment: bool = False
    allow_duet: bool = False
    allow_stitch: bool = False
    video_made_with_ai: bool = False
    commercial_content_type: Literal["none", "brand_organic", "brand_content"] = "none"
    is_brand_organic_post: bool = False
    brand_partner_promote: bool = False
    auto_add_music: bool = False
    photo_cover_index: int | None = Field(default=None, ge=0, le=34)
    video_cover_timestamp_ms: int | None = Field(default=None, ge=0, le=600_000)
    content_preview_confirmed: bool = False
    express_consent_given: bool = False


class PostIn(BaseModel):
    content: str = Field(default="", max_length=10_000)
    connection_ids: list[str] = Field(min_length=1, max_length=30)
    attachment_ids: list[str] = Field(default_factory=list, max_length=35)
    scheduled_at: datetime | None = None
    instagram_content_type: Literal["standard", "story"] = "standard"
    tiktok_cover_attachment_id: str | None = Field(default=None, max_length=36)
    instagram_cover_attachment_id: str | None = Field(default=None, max_length=36)
    instagram_audio_id: str | None = Field(default=None, max_length=255)
    instagram_audio_volume: int = Field(default=100, ge=0, le=100)
    instagram_video_volume: int = Field(default=100, ge=0, le=100)
    tiktok_settings: TikTokSettingsIn | None = None

    @field_validator("connection_ids", "attachment_ids")
    @classmethod
    def no_duplicates(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("IDs must be unique")
        return value
