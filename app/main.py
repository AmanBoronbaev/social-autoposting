from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import jwt
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models import Attachment, Connection, Delivery, Post, ProviderCredential, User
from app.providers import ProviderError, WhapiClient, ZernioClient, attachment_path, list_telegram_targets
from app.schemas import AdminUserIn, ConnectionIn, LoginIn, PostIn, ProviderCredentialIn, TokenOut
from app.security import (
    decrypt_credentials,
    encrypt_credentials,
    hash_password,
    issue_access_token,
    read_access_token,
    verify_password,
)
from app.settings import Settings, get_settings

# The cabinet has its own UI.  Do not publish an unauthenticated API catalogue
# that reveals internal provider-management endpoints.
app = FastAPI(title="Autoposting Platform", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
bearer = HTTPBearer(auto_error=False)
MEDIA_AUDIENCE = "whapi-media"
STATIC_DIR = Path(__file__).parent / "static"
ADMIN_ASSETS_DIR = Path(__file__).parent / "admin_assets"
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")
UPLOAD_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".avif": "image/avif",
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".webm": "video/webm",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".3gp": "video/3gpp",
    ".ts": "video/mp2t",
    ".m2ts": "video/mp2t",
    ".hevc": "video/hevc",
    ".pdf": "application/pdf",
}


def now() -> datetime:
    return datetime.now(UTC)


def normalized_upload_content_type(declared_type: str | None, original_name: str) -> str | None:
    """Prefer a browser's recognised media type, with a safe extension fallback.

    Some browsers, especially Safari, label otherwise supported camera files
    as a generic binary stream. The extension fallback accepts a small
    allow-list of image and video formats while still rejecting arbitrary
    executable/binary uploads.
    """
    content_type = (declared_type or "").lower().split(";", 1)[0].strip()
    if content_type.startswith("image/") or content_type.startswith("video/") or content_type == "application/pdf":
        return content_type
    return UPLOAD_CONTENT_TYPES.get(Path(original_name).suffix.lower())


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def settings_dependency() -> Settings:
    return get_settings()


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dependency),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
    try:
        user_id = read_access_token(credentials.credentials, settings)
    except jwt.InvalidTokenError as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid access token") from error
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user not found")
    return user


def current_superuser(user: User = Depends(current_user)) -> User:
    if not user.is_superuser:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="superuser role required")
    return user


def connection_dict(connection: Connection, *, include_provider: bool = True) -> dict:
    result = {
        "id": connection.id,
        "platform": connection.platform,
        "label": connection.label,
        "external_id": connection.external_id,
        "status": connection.status,
        "created_at": connection.created_at,
    }
    if include_provider:
        result["provider"] = connection.provider
    return result


def credential_dict(credential: ProviderCredential) -> dict:
    """A safe representation: it deliberately never contains the raw token."""
    return {
        "id": credential.id,
        "provider": credential.provider,
        "updated_at": credential.updated_at,
    }


def customer_provider_token(
    user_id: str, provider: str, db: Session, settings: Settings
) -> str:
    """Load a customer's encrypted service token without ever returning it."""
    if db.get(User, user_id) is None:
        raise HTTPException(status_code=404, detail="user not found")
    credential = db.scalar(
        select(ProviderCredential).where(
            ProviderCredential.user_id == user_id,
            ProviderCredential.provider == provider,
        )
    )
    if credential is None:
        raise HTTPException(status_code=422, detail=f"save the customer's {provider} token first")
    try:
        api_token = decrypt_credentials(credential.encrypted_credentials, settings).get("api_token", "")
    except ValueError as error:
        raise HTTPException(status_code=500, detail="stored provider credentials cannot be read") from error
    if not api_token:
        raise HTTPException(status_code=422, detail=f"the customer's {provider} token is empty")
    return api_token


def validate_connection_payload(payload: ConnectionIn) -> None:
    if payload.provider == "zernio" and payload.platform not in {"instagram", "tiktok"}:
        raise HTTPException(status_code=422, detail="Zernio requires an Instagram or TikTok platform")
    if payload.provider == "telegram" and payload.platform != "telegram":
        raise HTTPException(status_code=422, detail="Telegram provider requires telegram platform")
    if payload.provider == "whapi" and payload.platform != "whatsapp":
        raise HTTPException(status_code=422, detail="Whapi provider requires whatsapp platform")


def user_dict(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "is_superuser": user.is_superuser,
        "is_active": user.is_active,
        "created_at": user.created_at,
    }


def public_delivery_error(delivery: Delivery) -> str | None:
    """Return a useful, provider-neutral error for a customer cabinet.

    Raw delivery errors can contain names and response wording from third-party
    APIs. Those details stay in the database and are available to a
    superuser, but must not leak into a customer-facing history.
    """
    if delivery.status == "unknown":
        return "Статус публикации не подтверждён. Проверьте площадку перед повторной отправкой."
    if delivery.status != "failed":
        return None
    error = (delivery.error or "").lower()
    if "frame rate" in error:
        return "Видео должно иметь частоту от 23 до 60 кадров в секунду."
    if "conversion timed out" in error:
        return "Подготовка видео заняла слишком много времени. Загрузите более короткий файл."
    if "media file is missing" in error:
        return "Исходный файл больше недоступен. Загрузите его заново."
    return "Публикация не выполнена. Проверьте требования выбранной площадки и повторите попытку."


def delivery_dict(delivery: Delivery, *, include_internal_details: bool) -> dict:
    destination: dict | None = None
    if delivery.connection is not None:
        destination = {
            "label": delivery.connection.label,
            "platform": delivery.connection.platform,
        }
        if include_internal_details:
            destination["provider"] = delivery.connection.provider
    result = {
        "id": delivery.id,
        "connection_id": delivery.connection_id,
        "status": delivery.status,
        "attempts": delivery.attempts,
        "error": delivery.error if include_internal_details else public_delivery_error(delivery),
        "completed_at": delivery.completed_at,
        "destination": destination,
    }
    if include_internal_details:
        result["platform_options"] = delivery.platform_options
    return result


def post_dict(
    post: Post,
    settings: Settings | None = None,
    *,
    include_internal_details: bool = False,
) -> dict:
    return {
        "id": post.id,
        "content": post.content,
        "scheduled_at": post.scheduled_at,
        "attachments": [
            {
                "id": item.id,
                "name": item.original_name,
                "content_type": item.content_type,
                "size_bytes": item.size_bytes,
                "media_url": signed_media_url(item, settings) if settings is not None else None,
            }
            for item in post.attachments
            if (item.role or "media") == "media"
        ],
        "deliveries": [
            delivery_dict(item, include_internal_details=include_internal_details) for item in post.deliveries
        ],
    }


@app.get("/healthz", tags=["system"])
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/admin/ui", include_in_schema=False)
def admin_ui(admin: User = Depends(current_superuser)) -> FileResponse:
    """Serve admin-only markup instead of sending provider details to clients."""
    del admin
    return FileResponse(
        ADMIN_ASSETS_DIR / "admin.html",
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/v1/admin/client.js", include_in_schema=False)
def admin_client_script(admin: User = Depends(current_superuser)) -> FileResponse:
    """Serve provider-management code only to authenticated superusers."""
    del admin
    return FileResponse(
        ADMIN_ASSETS_DIR / "admin.js",
        media_type="text/javascript; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/v1/admin/users", tags=["admin"])
def list_users(admin: User = Depends(current_superuser), db: Session = Depends(get_db)) -> list[dict]:
    del admin
    return [user_dict(item) for item in db.scalars(select(User).order_by(User.created_at.desc()))]


@app.post("/v1/admin/users", status_code=status.HTTP_201_CREATED, tags=["admin"])
def create_user(
    payload: AdminUserIn,
    admin: User = Depends(current_superuser),
    db: Session = Depends(get_db),
) -> dict:
    del admin
    email = payload.email.lower()
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email is already registered")
    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name.strip(),
        is_superuser=payload.is_superuser,
    )
    db.add(user)
    db.flush()
    return user_dict(user)


@app.patch("/v1/admin/users/{user_id}/active", tags=["admin"])
def set_user_active(
    user_id: str,
    active: bool,
    admin: User = Depends(current_superuser),
    db: Session = Depends(get_db),
) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if user.id == admin.id and not active:
        raise HTTPException(status_code=422, detail="you cannot deactivate yourself")
    user.is_active = active
    return user_dict(user)


@app.post("/v1/auth/login", response_model=TokenOut, tags=["auth"])
def login(
    payload: LoginIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dependency),
) -> TokenOut:
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid email or password")
    return TokenOut(access_token=issue_access_token(user.id, settings))


@app.get("/v1/me", tags=["auth"])
def me(user: User = Depends(current_user)) -> dict:
    return user_dict(user)


@app.get("/v1/connections", tags=["connections"])
def list_connections(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    connections = db.scalars(
        select(Connection).where(Connection.user_id == user.id).order_by(Connection.created_at.desc())
    )
    return [connection_dict(item, include_provider=user.is_superuser) for item in connections]


@app.get("/v1/connections/{connection_id}/tiktok/creator-info", tags=["connections"])
def get_tiktok_creator_info(
    connection_id: str,
    media_type: str = "video",
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dependency),
) -> dict:
    if media_type not in {"video", "photo"}:
        raise HTTPException(status_code=422, detail="TikTok media type must be video or photo")
    connection = db.scalar(
        select(Connection).where(
            Connection.id == connection_id,
            Connection.user_id == user.id,
            Connection.provider == "zernio",
            Connection.platform == "tiktok",
        )
    )
    if connection is None:
        raise HTTPException(status_code=404, detail="TikTok destination not found")
    try:
        api_token = customer_provider_token(user.id, "zernio", db, settings)
        payload = ZernioClient(settings, api_token).get_tiktok_creator_info(connection.external_id, media_type)
    except HTTPException as error:
        if user.is_superuser:
            raise
        raise HTTPException(
            status_code=502,
            detail="Не удалось загрузить настройки TikTok. Попробуйте ещё раз позже.",
        ) from error
    except (ProviderError, ValueError) as error:
        detail = f"could not load TikTok account settings: {error}" if user.is_superuser else (
            "Не удалось загрузить настройки TikTok. Попробуйте ещё раз позже."
        )
        raise HTTPException(status_code=502, detail=detail) from error

    privacy_levels = payload.get("privacyLevels")
    if not isinstance(privacy_levels, list):
        raise HTTPException(status_code=502, detail="TikTok returned no allowed privacy levels")
    normalized_privacy = []
    for item in privacy_levels:
        if isinstance(item, dict) and isinstance(item.get("value"), str):
            normalized_privacy.append({"value": item["value"], "label": str(item.get("label") or item["value"])})
        elif isinstance(item, str):
            normalized_privacy.append({"value": item, "label": item})
    if not normalized_privacy:
        raise HTTPException(status_code=502, detail="TikTok returned no usable privacy levels")
    interaction_settings = payload.get("postingLimits", {})
    interactions = interaction_settings.get("interactionSettings", {}) if isinstance(interaction_settings, dict) else {}

    def interaction_default(*names: str) -> bool:
        if not isinstance(interactions, dict):
            return False
        for name in names:
            value = interactions.get(name)
            if isinstance(value, bool):
                return value
            if isinstance(value, dict):
                for key in ("default", "value", "enabled"):
                    if isinstance(value.get(key), bool):
                        return value[key]
        return False

    return {
        "privacy_levels": normalized_privacy,
        "interaction_defaults": {
            "allow_comment": interaction_default("allow_comment", "comment"),
            "allow_duet": interaction_default("allow_duet", "duet"),
            "allow_stitch": interaction_default("allow_stitch", "stitch"),
        },
        "commercial_content_types": [
            {"value": item["value"], "label": str(item.get("label") or item["value"])}
            for item in payload.get("commercialContentTypes", [])
            if isinstance(item, dict) and isinstance(item.get("value"), str)
        ],
    }


def normalized_instagram_audio(payload: dict) -> list[dict[str, str | int | None]]:
    """Normalize the catalogue response without returning third-party URLs."""
    candidates: object = None
    for key in ("audio", "audios", "items", "results", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            candidates = value
            break
        if isinstance(value, dict):
            for nested_key in ("audio", "audios", "items", "results", "data"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    candidates = nested
                    break
        if candidates is not None:
            break
    if not isinstance(candidates, list):
        return []
    result: list[dict[str, str | int | None]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        audio_id = item.get("audioId") or item.get("id") or item.get("_id")
        if not isinstance(audio_id, str) or not audio_id:
            continue
        duration = item.get("duration") or item.get("durationSeconds")
        result.append(
            {
                "id": audio_id,
                "title": str(item.get("title") or item.get("name") or "Без названия"),
                "artist": str(item.get("artist") or item.get("creator") or ""),
                "kind": str(item.get("audioType") or item.get("type") or ""),
                "duration": duration if isinstance(duration, int | float) else None,
            }
        )
    return result


@app.get("/v1/connections/{connection_id}/instagram/audio", tags=["connections"])
def search_instagram_audio(
    connection_id: str,
    audio_type: str = "music",
    q: str = "",
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dependency),
) -> dict:
    if audio_type not in {"music", "original_sound"}:
        raise HTTPException(status_code=422, detail="Тип аудио указан неверно.")
    if len(q) > 160:
        raise HTTPException(status_code=422, detail="Поисковый запрос слишком длинный.")
    connection = db.scalar(
        select(Connection).where(
            Connection.id == connection_id,
            Connection.user_id == user.id,
            Connection.provider == "zernio",
            Connection.platform == "instagram",
        )
    )
    if connection is None:
        raise HTTPException(status_code=404, detail="Instagram-площадка не найдена.")
    try:
        api_token = customer_provider_token(user.id, "zernio", db, settings)
        payload = ZernioClient(settings, api_token).search_instagram_audio(
            connection.external_id, audio_type=audio_type, query=q
        )
    except HTTPException as error:
        if user.is_superuser:
            raise
        raise HTTPException(
            status_code=502,
            detail="Музыка сейчас недоступна для этой Instagram-площадки. Проверьте её подключение.",
        ) from error
    except (ProviderError, ValueError) as error:
        detail = f"could not load Instagram audio: {error}" if user.is_superuser else (
            "Музыка сейчас недоступна для этой Instagram-площадки. Проверьте её подключение."
        )
        raise HTTPException(status_code=502, detail=detail) from error
    return {"tracks": normalized_instagram_audio(payload)}


@app.get("/v1/admin/users/{user_id}/connections", tags=["admin"])
def list_user_connections(
    user_id: str,
    admin: User = Depends(current_superuser),
    db: Session = Depends(get_db),
) -> list[dict]:
    del admin
    if db.get(User, user_id) is None:
        raise HTTPException(status_code=404, detail="user not found")
    return [
        connection_dict(item)
        for item in db.scalars(
            select(Connection).where(Connection.user_id == user_id).order_by(Connection.created_at.desc())
        )
    ]


@app.get("/v1/admin/users/{user_id}/credentials", tags=["admin"])
def list_user_credentials(
    user_id: str,
    admin: User = Depends(current_superuser),
    db: Session = Depends(get_db),
) -> list[dict]:
    del admin
    if db.get(User, user_id) is None:
        raise HTTPException(status_code=404, detail="user not found")
    credentials = db.scalars(
        select(ProviderCredential)
        .where(ProviderCredential.user_id == user_id)
        .order_by(ProviderCredential.provider)
    )
    return [credential_dict(item) for item in credentials]


@app.post("/v1/admin/users/{user_id}/credentials", tags=["admin"])
def upsert_user_credential(
    user_id: str,
    payload: ProviderCredentialIn,
    admin: User = Depends(current_superuser),
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dependency),
) -> dict:
    del admin
    if db.get(User, user_id) is None:
        raise HTTPException(status_code=404, detail="user not found")
    credential = db.scalar(
        select(ProviderCredential).where(
            ProviderCredential.user_id == user_id,
            ProviderCredential.provider == payload.provider,
        )
    )
    encrypted = encrypt_credentials({"api_token": payload.api_token}, settings)
    if credential is None:
        credential = ProviderCredential(
            user_id=user_id,
            provider=payload.provider,
            encrypted_credentials=encrypted,
        )
        db.add(credential)
    else:
        credential.encrypted_credentials = encrypted
    db.flush()
    return credential_dict(credential)


@app.get("/v1/admin/users/{user_id}/zernio/accounts", tags=["admin"])
def list_user_zernio_accounts(
    user_id: str,
    admin: User = Depends(current_superuser),
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dependency),
) -> list[dict]:
    """List connected Instagram/TikTok accounts without exposing the API key."""
    del admin
    if db.get(User, user_id) is None:
        raise HTTPException(status_code=404, detail="user not found")
    credential = db.scalar(
        select(ProviderCredential).where(
            ProviderCredential.user_id == user_id,
            ProviderCredential.provider == "zernio",
        )
    )
    if credential is None:
        raise HTTPException(status_code=422, detail="save the customer's Zernio token first")
    try:
        api_token = decrypt_credentials(credential.encrypted_credentials, settings).get("api_token", "")
        accounts = ZernioClient(settings, api_token).list_accounts()
    except (ProviderError, ValueError) as error:
        raise HTTPException(status_code=502, detail=f"could not load Zernio accounts: {error}") from error
    result = []
    for account in accounts:
        account_id = account.get("_id") or account.get("id")
        platform = account.get("platform")
        if not isinstance(account_id, str) or platform not in {"instagram", "tiktok"}:
            continue
        profile = account.get("profileId")
        profile_name = profile.get("name") if isinstance(profile, dict) else None
        result.append(
            {
                "id": account_id,
                "platform": platform,
                "username": str(account.get("username") or ""),
                "display_name": str(account.get("displayName") or account.get("username") or account_id),
                "profile_name": profile_name if isinstance(profile_name, str) else "",
                "status": "connected" if account.get("isActive", True) else "disconnected",
            }
        )
    return result


@app.get("/v1/admin/users/{user_id}/telegram/targets", tags=["admin"])
def list_user_telegram_targets(
    user_id: str,
    admin: User = Depends(current_superuser),
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dependency),
) -> list[dict]:
    """Discover destinations from Bot API updates; Telegram cannot list all bot chats."""
    del admin
    api_token = customer_provider_token(user_id, "telegram", db, settings)
    try:
        return list_telegram_targets(settings, api_token)
    except ProviderError as error:
        raise HTTPException(status_code=502, detail=f"could not find Telegram chats: {error}") from error


@app.get("/v1/admin/users/{user_id}/whapi/targets", tags=["admin"])
def list_user_whapi_targets(
    user_id: str,
    admin: User = Depends(current_superuser),
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dependency),
) -> list[dict]:
    """List groups and WhatsApp Channels available to the customer's Whapi token."""
    del admin
    api_token = customer_provider_token(user_id, "whapi", db, settings)
    try:
        return WhapiClient(api_token).list_targets()
    except ProviderError as error:
        raise HTTPException(status_code=502, detail=f"could not load WhatsApp destinations: {error}") from error


@app.post("/v1/admin/users/{user_id}/connections", status_code=status.HTTP_201_CREATED, tags=["admin"])
def create_user_connection(
    user_id: str,
    payload: ConnectionIn,
    admin: User = Depends(current_superuser),
    db: Session = Depends(get_db),
) -> dict:
    del admin
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    validate_connection_payload(payload)
    credential_exists = db.scalar(
        select(ProviderCredential.id).where(
            ProviderCredential.user_id == user_id,
            ProviderCredential.provider == payload.provider,
        )
    )
    if credential_exists is None:
        raise HTTPException(status_code=422, detail="add the customer's provider token before adding a destination")
    connection = Connection(
        user_id=user.id,
        provider=payload.provider,
        platform=payload.platform,
        label=payload.label.strip(),
        external_id=payload.external_id,
    )
    db.add(connection)
    try:
        db.flush()
    except IntegrityError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="destination already exists") from error
    return connection_dict(connection)


@app.post("/v1/uploads", status_code=status.HTTP_201_CREATED, tags=["media"])
async def upload(
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dependency),
) -> dict:
    original_name = Path(file.filename or "upload").name
    content_type = normalized_upload_content_type(file.content_type, original_name)
    if content_type is None:
        raise HTTPException(status_code=415, detail="only image, video and PDF uploads are accepted")
    storage_key = f"{user.id}/{uuid4().hex}"
    destination = settings.media_dir / storage_key
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    size = 0
    try:
        with destination.open("xb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(status_code=413, detail="upload exceeds configured limit")
                output.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    attachment = Attachment(
        user_id=user.id,
        original_name=original_name,
        content_type=content_type,
        size_bytes=size,
        storage_key=storage_key,
    )
    db.add(attachment)
    db.flush()
    return {"id": attachment.id, "name": attachment.original_name, "size_bytes": attachment.size_bytes}


@app.post("/v1/posts", status_code=status.HTTP_201_CREATED, tags=["posts"])
def create_post(
    payload: PostIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dependency),
) -> dict:
    if not payload.content and not payload.attachment_ids:
        raise HTTPException(status_code=422, detail="post needs text or an attachment")
    scheduled_at = payload.scheduled_at or now()
    if scheduled_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="scheduled_at must include a timezone")
    if scheduled_at < now() - timedelta(minutes=1):
        raise HTTPException(status_code=422, detail="scheduled_at is in the past")
    connections = list(
        db.scalars(select(Connection).where(Connection.id.in_(payload.connection_ids), Connection.user_id == user.id))
    )
    if len(connections) != len(payload.connection_ids):
        raise HTTPException(status_code=404, detail="one or more destinations were not found")
    attachments = list(
        db.scalars(
            select(Attachment).where(
                Attachment.id.in_(payload.attachment_ids),
                Attachment.user_id == user.id,
                Attachment.post_id.is_(None),
                Attachment.role == "media",
            )
        )
    )
    if len(attachments) != len(payload.attachment_ids):
        raise HTTPException(status_code=404, detail="one or more uploads were not found or already used")
    cover_ids = {
        attachment_id
        for attachment_id in (payload.tiktok_cover_attachment_id, payload.instagram_cover_attachment_id)
        if attachment_id
    }
    if cover_ids.intersection(payload.attachment_ids):
        raise HTTPException(status_code=422, detail="an image cannot be both post media and a custom cover")
    cover_attachments = list(
        db.scalars(
            select(Attachment).where(
                Attachment.id.in_(cover_ids),
                Attachment.user_id == user.id,
                Attachment.post_id.is_(None),
                Attachment.role == "media",
            )
        )
    )
    if len(cover_attachments) != len(cover_ids):
        raise HTTPException(status_code=404, detail="one or more custom cover uploads were not found or already used")
    covers_by_id = {item.id: item for item in cover_attachments}
    tiktok_cover = covers_by_id.get(payload.tiktok_cover_attachment_id or "")
    instagram_cover = covers_by_id.get(payload.instagram_cover_attachment_id or "")
    if any(item.provider == "zernio" for item in connections) and any(
        not (attachment.content_type.startswith("image/") or attachment.content_type.startswith("video/"))
        for attachment in attachments
    ):
        raise HTTPException(status_code=422, detail="Instagram and TikTok accept only images or video, not documents")
    instagram_connections = [item for item in connections if item.provider == "zernio" and item.platform == "instagram"]
    if instagram_connections and not attachments:
        raise HTTPException(status_code=422, detail="Instagram needs an image or video attachment")
    if instagram_connections and len(attachments) > 10:
        raise HTTPException(status_code=422, detail="Instagram accepts at most 10 carousel items")
    story = payload.instagram_content_type == "story"
    if story:
        if not attachments:
            raise HTTPException(status_code=422, detail="an Instagram story needs one image or video")
        if len(attachments) != 1 or not (
            attachments[0].content_type.startswith("image/") or attachments[0].content_type.startswith("video/")
        ):
            raise HTTPException(status_code=422, detail="an Instagram story accepts exactly one image or video")
        if any(item.provider != "zernio" or item.platform != "instagram" for item in connections):
            raise HTTPException(status_code=422, detail="an Instagram story can target only Instagram accounts")
    tiktok_connections = [item for item in connections if item.provider == "zernio" and item.platform == "tiktok"]
    is_video = bool(attachments) and all(item.content_type.startswith("video/") for item in attachments)
    is_photo = bool(attachments) and all(item.content_type.startswith("image/") for item in attachments)
    if tiktok_connections:
        if len(tiktok_connections) > 1:
            raise HTTPException(status_code=422, detail="select only one TikTok account per post")
        if payload.tiktok_settings is None:
            raise HTTPException(status_code=422, detail="TikTok settings and confirmations are required")
        if not payload.tiktok_settings.content_preview_confirmed or not payload.tiktok_settings.express_consent_given:
            raise HTTPException(status_code=422, detail="TikTok confirmations are required")
        if not attachments:
            raise HTTPException(status_code=422, detail="a TikTok post needs an image or video")
        if not is_video and not is_photo:
            raise HTTPException(status_code=422, detail="TikTok accepts either images or one video, not mixed files")
        if is_video and len(attachments) != 1:
            raise HTTPException(status_code=422, detail="a TikTok post accepts exactly one video")
    if tiktok_cover is not None:
        if not tiktok_connections:
            raise HTTPException(status_code=422, detail="a TikTok cover requires a TikTok destination")
        if not is_video:
            raise HTTPException(status_code=422, detail="a TikTok custom cover requires exactly one video")
        if tiktok_cover.content_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise HTTPException(status_code=422, detail="a TikTok cover must be a JPG, PNG or WebP image")
        if tiktok_cover.size_bytes > 20 * 1024 * 1024:
            raise HTTPException(status_code=422, detail="a TikTok cover must not exceed 20 MB")
    single_instagram_video = (
        len(attachments) == 1 and attachments[0].content_type.startswith("video/") and not story
    )
    if instagram_cover is not None:
        if not instagram_connections:
            raise HTTPException(status_code=422, detail="an Instagram cover requires an Instagram destination")
        if not single_instagram_video:
            raise HTTPException(status_code=422, detail="an Instagram custom cover requires one Reel video")
        if instagram_cover.content_type not in {"image/jpeg", "image/png"}:
            raise HTTPException(status_code=422, detail="an Instagram cover must be a JPG or PNG image")
        if instagram_cover.size_bytes > 20 * 1024 * 1024:
            raise HTTPException(status_code=422, detail="an Instagram cover must not exceed 20 MB")
    instagram_audio_id = (payload.instagram_audio_id or "").strip()
    if instagram_audio_id:
        if len(instagram_connections) != 1:
            raise HTTPException(status_code=422, detail="music can be selected for one Instagram destination at a time")
        if not single_instagram_video:
            raise HTTPException(status_code=422, detail="Instagram music is available only for one Reel video")
    post = Post(user_id=user.id, content=payload.content, scheduled_at=scheduled_at)
    db.add(post)
    db.flush()
    for attachment in attachments:
        attachment.post_id = post.id
        attachment.role = "media"
    for attachment in cover_attachments:
        attachment.post_id = post.id
        attachment.role = "cover"
    for connection in connections:
        platform_options = {"contentType": "story"} if story else {}
        if connection.platform == "tiktok" and payload.tiktok_settings is not None:
            tiktok_settings = payload.tiktok_settings.model_dump()
            tiktok_settings["media_type"] = "video" if is_video else "photo"
            if is_photo:
                tiktok_settings["description"] = payload.content
            if tiktok_cover is not None:
                tiktok_settings["video_cover_attachment_id"] = tiktok_cover.id
            platform_options["tiktokSettings"] = tiktok_settings
        if connection.platform == "instagram":
            if instagram_cover is not None:
                platform_options["instagramThumbnailAttachmentId"] = instagram_cover.id
            if instagram_audio_id:
                platform_options["audioConfiguration"] = {
                    "audioId": instagram_audio_id,
                    "audioVolume": payload.instagram_audio_volume,
                    "videoVolume": payload.instagram_video_volume,
                }
        db.add(
            Delivery(
                post_id=post.id,
                connection_id=connection.id,
                available_at=scheduled_at,
                platform_options=platform_options or None,
            )
        )
    db.flush()
    post = db.scalar(
        select(Post)
        .options(selectinload(Post.attachments), selectinload(Post.deliveries).selectinload(Delivery.connection))
        .where(Post.id == post.id)
    )
    assert post is not None
    return post_dict(post, settings, include_internal_details=user.is_superuser)


@app.get("/v1/posts/{post_id}", tags=["posts"])
def get_post(
    post_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dependency),
) -> dict:
    post = db.scalar(
        select(Post)
        .options(selectinload(Post.attachments), selectinload(Post.deliveries).selectinload(Delivery.connection))
        .where(Post.id == post_id, Post.user_id == user.id)
    )
    if post is None:
        raise HTTPException(status_code=404, detail="post not found")
    return post_dict(post, settings, include_internal_details=user.is_superuser)


@app.get("/v1/posts", tags=["posts"])
def list_posts(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dependency),
) -> list[dict]:
    posts = db.scalars(
        select(Post)
        .options(selectinload(Post.attachments), selectinload(Post.deliveries).selectinload(Delivery.connection))
        .where(Post.user_id == user.id)
        .order_by(Post.scheduled_at.desc())
        .limit(100)
    )
    return [post_dict(item, settings, include_internal_details=user.is_superuser) for item in posts]


@app.post("/v1/deliveries/{delivery_id}/retry", tags=["posts"])
def retry_delivery(delivery_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    delivery = db.scalar(
        select(Delivery).join(Delivery.post).where(Delivery.id == delivery_id, Post.user_id == user.id)
    )
    if delivery is None:
        raise HTTPException(status_code=404, detail="delivery not found")
    if delivery.status not in {"failed", "unknown"}:
        raise HTTPException(status_code=422, detail="only failed or unknown deliveries can be retried")
    previous_status = delivery.status
    delivery.status = "queued"
    delivery.available_at = now()
    delivery.locked_at = None
    delivery.error = None
    # A final Zernio failure is known not to have been published. Submit it as
    # a fresh Zernio post on manual retry so corrected media is not confused
    # with the old terminal status. Keep `unknown` deliveries unchanged: they
    # may have reached the platform before a worker crash and must be checked.
    if previous_status == "failed" and delivery.connection.provider == "zernio":
        delivery.provider_response = {"_retry_request_id": uuid4().hex}
    return {"id": delivery.id, "status": delivery.status}


@app.get("/v1/media/{attachment_id}", include_in_schema=False)
def public_media(
    attachment_id: str,
    token: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dependency),
) -> FileResponse:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            audience=MEDIA_AUDIENCE,
        )
    except jwt.InvalidTokenError as error:
        raise HTTPException(status_code=404, detail="media not found") from error
    if payload.get("attachment_id") != attachment_id:
        raise HTTPException(status_code=404, detail="media not found")
    attachment = db.get(Attachment, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=404, detail="media not found")
    try:
        path = attachment_path(attachment, settings)
    except ProviderError as error:
        raise HTTPException(status_code=404, detail="media not found") from error
    return FileResponse(path, media_type=attachment.content_type, filename=attachment.original_name)


def signed_media_url(attachment: Attachment, settings: Settings) -> str:
    token = jwt.encode(
        {"attachment_id": attachment.id, "aud": MEDIA_AUDIENCE, "exp": now() + timedelta(minutes=15)},
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    return f"{settings.path_prefix}/v1/media/{attachment.id}?token={token}"
