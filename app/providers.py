import base64
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

from app.models import Attachment, Connection, Delivery, Post
from app.settings import Settings


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class PublishResult:
    payload: dict[str, Any]


@dataclass(frozen=True)
class PreparedAttachment:
    """A provider-compatible source file, optionally created just for one delivery."""

    path: Path
    content_type: str
    original_name: str


def attachment_path(attachment: Attachment, settings: Settings) -> Path:
    candidate = (settings.media_dir / attachment.storage_key).resolve()
    root = settings.media_dir.resolve()
    if root not in candidate.parents or not candidate.is_file():
        raise ProviderError("media file is missing")
    return candidate


@contextmanager
def prepared_attachment(attachment: Attachment, settings: Settings) -> Iterator[PreparedAttachment]:
    """Normalize every uploaded video to the MP4/H.264/AAC baseline.

    The same original may go to several platforms. Keeping the original in the
    database and preparing a temporary delivery copy avoids losing it while
    making WhatsApp, Instagram and TikTok receive a broadly compatible video.
    """
    source = attachment_path(attachment, settings)
    original = PreparedAttachment(source, attachment.content_type, attachment.original_name)
    if not attachment.content_type.startswith("video/"):
        yield original
        return

    if shutil.which("ffmpeg") is None:
        raise ProviderError("ffmpeg is required to prepare video for social platforms")
    temporary_dir = settings.media_dir / ".prepared"
    temporary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    output = temporary_dir / f"{uuid4().hex}.mp4"
    try:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                str(output),
            ],
            capture_output=True,
            timeout=settings.media_prepare_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        output.unlink(missing_ok=True)
        raise ProviderError("video conversion timed out") from error
    if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        output.unlink(missing_ok=True)
        detail = completed.stderr.decode(errors="replace").strip()[:500]
        raise ProviderError(f"video conversion failed{f': {detail}' if detail else ''}")
    name = f"{Path(attachment.original_name).stem or 'video'}.mp4"
    try:
        yield PreparedAttachment(output, "video/mp4", name)
    finally:
        output.unlink(missing_ok=True)


def _response_json(response: httpx.Response, secret: str = "") -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        payload = {"detail": response.text[:500]}
    safe = str(payload).replace(secret, "***") if secret else str(payload)
    if response.is_error:
        raise ProviderError(
            f"provider returned {response.status_code}: {safe[:700]}",
            retryable=response.status_code >= 500 or response.status_code == 429,
        )
    return payload if isinstance(payload, dict) else {"result": payload}


def _safe_error(error: Exception, secret: str) -> str:
    """Do not accidentally expose a provider credential in an HTTP exception."""
    return str(error).replace(secret, "***")


class ZernioClient:
    def __init__(self, settings: Settings, api_token: str) -> None:
        if not api_token:
            raise ProviderError("Zernio token is missing for this customer")
        self.settings = settings
        self.key = api_token
        self.base_url = str(settings.zernio_api_base_url).rstrip("/")

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        extra_headers = kwargs.pop("headers", {})
        try:
            with httpx.Client(timeout=60) as client:
                response = client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers={**self.headers, **extra_headers},
                    **kwargs,
                )
        except httpx.HTTPError as error:
            raise ProviderError(f"Zernio network error: {_safe_error(error, self.key)}", retryable=True) from error
        return _response_json(response, self.key)

    def upload_media(self, attachment: PreparedAttachment) -> dict[str, str]:
        presign = self._request(
            "POST",
            "/media/presign",
            json={"filename": attachment.original_name, "contentType": attachment.content_type},
        )
        upload_url, public_url = presign.get("uploadUrl"), presign.get("publicUrl")
        if not isinstance(upload_url, str) or not isinstance(public_url, str):
            raise ProviderError("Zernio did not return media upload URLs")
        try:
            with attachment.path.open("rb") as file_handle, httpx.Client(timeout=180) as client:
                response = client.put(
                    upload_url,
                    content=file_handle,
                    headers={"Content-Type": attachment.content_type},
                )
                response.raise_for_status()
        except httpx.HTTPError as error:
            raise ProviderError(
                f"Zernio media upload failed: {_safe_error(error, upload_url)}", retryable=True
            ) from error
        media_type = "video" if attachment.content_type.startswith("video/") else "image"
        return {"url": public_url, "type": media_type}

    def list_accounts(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/accounts", params={"status": "connected"})
        accounts = payload.get("accounts")
        if not isinstance(accounts, list):
            raise ProviderError("Zernio returned an invalid accounts list")
        return [item for item in accounts if isinstance(item, dict)]

    def get_post(self, post_id: str) -> dict[str, Any]:
        if not post_id:
            raise ProviderError("Zernio post ID is missing")
        return self._request("GET", f"/posts/{post_id}")

    def get_tiktok_creator_info(self, account_id: str, media_type: str) -> dict[str, Any]:
        if not account_id:
            raise ProviderError("TikTok account ID is missing")
        if media_type not in {"video", "photo"}:
            raise ProviderError("TikTok media type must be video or photo")
        return self._request(
            "GET",
            f"/accounts/{account_id}/tiktok/creator-info",
            params={"mediaType": media_type},
        )

    def publish(self, delivery: Delivery, post: Post, connection: Connection) -> PublishResult:
        media = []
        for item in post.attachments:
            with prepared_attachment(item, self.settings) as prepared:
                media.append(self.upload_media(prepared))
        platform: dict[str, Any] = {"platform": connection.platform, "accountId": connection.external_id}
        platform_options = delivery.platform_options or {}
        # TikTok settings are a documented top-level exception. Passing a
        # nested `tiktokSettings` in platformSpecificData is ignored by Zernio
        # and risks confusing future API versions.
        platform_specific_data = {
            key: value for key, value in platform_options.items() if key != "tiktokSettings"
        }
        if platform_specific_data:
            platform["platformSpecificData"] = platform_specific_data
        payload: dict[str, Any] = {
            "content": post.content,
            "platforms": [platform],
            "publishNow": True,
        }
        if connection.platform == "tiktok":
            tiktok_settings = platform_options.get("tiktokSettings")
            if not isinstance(tiktok_settings, dict):
                raise ProviderError("TikTok publication needs confirmed TikTok settings")
            payload["tiktokSettings"] = tiktok_settings
        if media:
            payload["mediaItems"] = media
        response = self._request(
            "POST",
            "/posts",
            json=payload,
            headers={**self.headers, "x-request-id": delivery.id},
        )
        return PublishResult(response)


TELEGRAM_DISCOVERY_UPDATE_TYPES = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "my_chat_member",
    "chat_member",
    "chat_join_request",
)


def list_telegram_targets(settings: Settings, bot_token: str) -> list[dict[str, str]]:
    """Return group/channel chats visible to a bot through pending Bot API updates.

    Telegram does not provide a Bot API method that lists every chat a bot has
    been added to.  A group command, a channel post, or a membership update is
    therefore used as an explicit, safe discovery signal.
    """
    if not bot_token:
        raise ProviderError("Telegram bot token is missing for this customer")
    base = str(settings.telegram_api_base_url).rstrip("/")
    try:
        with httpx.Client(timeout=20) as client:
            response = client.post(
                f"{base}/bot{bot_token}/getUpdates",
                json={"timeout": 0, "allowed_updates": list(TELEGRAM_DISCOVERY_UPDATE_TYPES)},
            )
    except httpx.HTTPError as error:
        raise ProviderError(f"Telegram network error: {_safe_error(error, bot_token)}", retryable=True) from error
    payload = _response_json(response, bot_token)
    updates = payload.get("result")
    if not isinstance(updates, list):
        raise ProviderError("Telegram returned an invalid updates list")

    found: dict[str, dict[str, str]] = {}
    for update in updates:
        if not isinstance(update, dict):
            continue
        for field in TELEGRAM_DISCOVERY_UPDATE_TYPES:
            event = update.get(field)
            chat = _telegram_target_chat(event)
            if not isinstance(chat, dict):
                continue
            chat_id = chat.get("id")
            chat_type = chat.get("type")
            if chat_id is None or chat_type not in {"group", "supergroup", "channel"}:
                continue
            chat_id_text = str(chat_id)
            username = chat.get("username")
            title = chat.get("title") or (f"@{username}" if isinstance(username, str) else None) or chat_id_text
            found[chat_id_text] = {
                "id": chat_id_text,
                "kind": "channel" if chat_type == "channel" else "group",
                "label": str(title),
            }
    return sorted(found.values(), key=lambda item: (item["kind"], item["label"].casefold()))


def _telegram_target_chat(event: Any) -> dict[str, Any] | None:
    """Extract a target chat from a direct update or a post forwarded to the bot."""
    if not isinstance(event, dict):
        return None
    chat = event.get("chat")
    if isinstance(chat, dict) and chat.get("type") in {"group", "supergroup", "channel"}:
        return chat

    # A private message sent to the bot is not itself a publishing target.  But
    # Telegram exposes the source channel for a forwarded channel post, which
    # makes forwarding a post a convenient no-ID fallback for discovery.
    origin = event.get("forward_origin")
    if isinstance(origin, dict):
        origin_chat = origin.get("chat") if origin.get("type") == "channel" else origin.get("sender_chat")
        if isinstance(origin_chat, dict) and origin_chat.get("type") in {"group", "supergroup", "channel"}:
            return origin_chat
    legacy_origin = event.get("forward_from_chat")
    if isinstance(legacy_origin, dict) and legacy_origin.get("type") in {"group", "supergroup", "channel"}:
        return legacy_origin
    return None


def _items_from_provider_payload(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Accept the documented response as well as harmless pagination wrappers."""
    candidates = (payload.get(key), payload.get("data"), payload.get("result"))
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
        if isinstance(candidate, dict):
            nested = candidate.get(key) or candidate.get("items")
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    return []


class WhapiClient:
    """Small read-only part of Whapi used for the destination picker."""

    base_url = "https://gate.whapi.cloud"

    def __init__(self, api_token: str) -> None:
        if not api_token:
            raise ProviderError("Whapi token is missing for this customer")
        self.api_token = api_token

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_token}"}

    def _request(self, path: str) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=30) as client:
                response = client.get(f"{self.base_url}{path}", headers=self.headers)
        except httpx.HTTPError as error:
            raise ProviderError(
                f"Whapi network error: {_safe_error(error, self.api_token)}", retryable=True
            ) from error
        return _response_json(response, self.api_token)

    def list_targets(self) -> list[dict[str, str]]:
        groups = _items_from_provider_payload(self._request("/groups"), "groups")
        newsletters = _items_from_provider_payload(self._request("/newsletters"), "newsletters")
        found: dict[str, dict[str, str]] = {}
        for kind, items in (("group", groups), ("channel", newsletters)):
            for item in items:
                target_id = item.get("id") or item.get("chat_id") or item.get("jid")
                if target_id is None:
                    continue
                target_id_text = str(target_id)
                label = (
                    item.get("name")
                    or item.get("subject")
                    or item.get("title")
                    or item.get("display_name")
                    or target_id_text
                )
                found[target_id_text] = {"id": target_id_text, "kind": kind, "label": str(label)}
        return sorted(found.values(), key=lambda item: (item["kind"], item["label"].casefold()))


def _telegram_method(content_type: str) -> tuple[str, str]:
    if content_type.startswith("image/"):
        return "sendPhoto", "photo"
    if content_type.startswith("video/"):
        return "sendVideo", "video"
    return "sendDocument", "document"


def publish_telegram(post: Post, connection: Connection, settings: Settings, bot_token: str) -> PublishResult:
    if not bot_token:
        raise ProviderError("Telegram bot token is missing for this customer")
    base = str(settings.telegram_api_base_url).rstrip("/")
    result: list[dict[str, Any]] = []
    try:
        with httpx.Client(timeout=180) as client:
            if not post.attachments:
                response = client.post(
                    f"{base}/bot{bot_token}/sendMessage",
                    json={"chat_id": connection.external_id, "text": post.content},
                )
                result.append(_response_json(response, bot_token))
            else:
                for index, attachment in enumerate(post.attachments):
                    with prepared_attachment(attachment, settings) as prepared:
                        if prepared.path.stat().st_size > settings.telegram_max_upload_bytes:
                            raise ProviderError("Telegram attachment exceeds configured upload limit")
                        method, field = _telegram_method(prepared.content_type)
                        data = {"chat_id": connection.external_id}
                        if index == 0 and post.content:
                            data["caption"] = post.content
                        with prepared.path.open("rb") as file_handle:
                            response = client.post(
                                f"{base}/bot{bot_token}/{method}",
                                data=data,
                                files={field: (prepared.original_name, file_handle, prepared.content_type)},
                            )
                        result.append(_response_json(response, bot_token))
    except httpx.HTTPError as error:
        raise ProviderError(f"Telegram network error: {_safe_error(error, bot_token)}") from error
    return PublishResult({"messages": result})


def _whapi_endpoint(content_type: str) -> str:
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/"):
        return "video"
    return "document"


def _whapi_media_data_uri(attachment: PreparedAttachment, settings: Settings) -> str:
    size = attachment.path.stat().st_size
    if size > settings.whapi_max_media_bytes:
        raise ProviderError("attachment exceeds configured WhatsApp media limit")
    encoded = base64.b64encode(attachment.path.read_bytes()).decode("ascii")
    filename = quote(attachment.original_name, safe="._-")
    return f"data:{attachment.content_type};name={filename};base64,{encoded}"


def publish_whapi(
    post: Post,
    connection: Connection,
    settings: Settings,
    api_token: str,
) -> PublishResult:
    if not api_token:
        raise ProviderError("Whapi token is missing for this customer")
    result: list[dict[str, Any]] = []
    headers = {"Authorization": f"Bearer {api_token}"}
    try:
        with httpx.Client(timeout=180) as client:
            if not post.attachments:
                response = client.post(
                    "https://gate.whapi.cloud/messages/text",
                    headers=headers,
                    json={"to": connection.external_id, "body": post.content},
                )
                result.append(_response_json(response, api_token))
            else:
                for index, attachment in enumerate(post.attachments):
                    with prepared_attachment(attachment, settings) as prepared:
                        payload = {
                            "to": connection.external_id,
                            "media": _whapi_media_data_uri(prepared, settings),
                        }
                        if index == 0 and post.content:
                            payload["caption"] = post.content
                        response = client.post(
                            f"https://gate.whapi.cloud/messages/{_whapi_endpoint(prepared.content_type)}",
                            headers=headers,
                            json=payload,
                        )
                        result.append(_response_json(response, api_token))
    except httpx.HTTPError as error:
        raise ProviderError(f"Whapi network error: {_safe_error(error, api_token)}") from error
    return PublishResult({"messages": result})
