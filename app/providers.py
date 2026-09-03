import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
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


def post_media_attachments(post: Post) -> list[Attachment]:
    """Return publication media, excluding images saved only as custom covers."""
    media = [item for item in post.attachments if (item.role or "media") == "media"]
    # Also sort here for in-memory posts used by the worker and tests. Database
    # relationship loading has the same order configured on ``Post``.
    return sorted(media, key=lambda item: item.position or 0)


def post_cover_attachment(post: Post, attachment_id: str) -> Attachment:
    for item in post.attachments:
        if item.id == attachment_id and item.role == "cover":
            return item
    raise ProviderError("custom cover image is missing")


def attachment_path(attachment: Attachment, settings: Settings) -> Path:
    candidate = (settings.media_dir / attachment.storage_key).resolve()
    root = settings.media_dir.resolve()
    if root not in candidate.parents or not candidate.is_file():
        raise ProviderError("media file is missing")
    return candidate


def _video_duration_seconds(source: Path, settings: Settings) -> float:
    """Read a prepared video's duration without loading media into memory."""
    if shutil.which("ffprobe") is None:
        raise ProviderError("ffprobe is required to fit video to a media limit")
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(source),
            ],
            capture_output=True,
            timeout=settings.media_prepare_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ProviderError("video conversion timed out") from error
    if completed.returncode != 0:
        raise ProviderError("could not determine video duration for compression")
    try:
        duration = float(completed.stdout.decode(errors="replace").strip())
    except ValueError as error:
        raise ProviderError("could not determine video duration for compression") from error
    if duration <= 0:
        raise ProviderError("could not determine video duration for compression")
    return duration


def _video_scale_filter(video_bit_rate: int) -> str:
    """Trade resolution for a usable result when the available bitrate is tiny."""
    if video_bit_rate < 250_000:
        max_width = 640
    elif video_bit_rate < 500_000:
        max_width = 854
    else:
        max_width = 1920
    return (
        f"scale=w='min({max_width},iw)':h='min({max_width},ih)':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2"
    )


def _fit_video_to_limit(source: Path, max_bytes: int, settings: Settings) -> Path:
    """Create a bounded MP4 without truncating the video's duration.

    Normalisation intentionally prefers quality. When a provider has a hard
    final-file limit, use a two-pass re-encode only for that delivery. The
    target includes a margin for audio and MP4 container overhead; a second
    lower target handles unusual source streams conservatively.
    """
    if source.stat().st_size <= max_bytes:
        return source
    duration = _video_duration_seconds(source, settings)
    temporary_dir = source.parent
    for budget_ratio in (0.92, 0.80):
        total_bit_rate = max(160_000, int(max_bytes * 8 * budget_ratio / duration))
        audio_bit_rate = min(128_000, max(32_000, total_bit_rate // 8))
        video_bit_rate = max(96_000, total_bit_rate - audio_bit_rate)
        output = temporary_dir / f"{uuid4().hex}-limited.mp4"
        passlog = temporary_dir / f"{uuid4().hex}-pass"
        common = [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-vf",
            _video_scale_filter(video_bit_rate),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-b:v",
            str(video_bit_rate),
            "-maxrate",
            str(video_bit_rate),
            "-bufsize",
            str(video_bit_rate * 2),
            "-pix_fmt",
            "yuv420p",
            "-passlogfile",
            str(passlog),
        ]
        try:
            first_pass = subprocess.run(
                [*common, "-an", "-pass", "1", "-f", "mp4", os.devnull],
                capture_output=True,
                timeout=settings.media_prepare_timeout_seconds,
                check=False,
            )
            second_pass = subprocess.run(
                [
                    *common,
                    "-map",
                    "0:a?",
                    "-c:a",
                    "aac",
                    "-b:a",
                    str(audio_bit_rate),
                    "-pass",
                    "2",
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
        finally:
            for log_file in temporary_dir.glob(f"{passlog.name}*"):
                log_file.unlink(missing_ok=True)
        if second_pass.returncode == 0 and output.is_file() and output.stat().st_size <= max_bytes:
            return output
        output.unlink(missing_ok=True)
        if first_pass.returncode != 0 or second_pass.returncode != 0:
            continue
    raise ProviderError("video cannot be compressed below the WhatsApp media limit")


@contextmanager
def prepared_attachment(
    attachment: Attachment, settings: Settings, *, max_bytes: int | None = None
) -> Iterator[PreparedAttachment]:
    """Normalize every uploaded video to an MP4/H.264/AAC, constant-30-FPS baseline.

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
                # TikTok rejects frame rates outside 23–60 FPS. Re-encoding at
                # a constant 30 FPS also handles variable-frame-rate phone
                # videos. Limit a 4K source to 1080p so large uploads become
                # practical for the social providers without changing normal
                # 720p/1080p uploads.
                "-vf",
                "fps=30,scale=w='min(1920,iw)':h='min(1920,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
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
    fitted_output: Path | None = None
    try:
        if max_bytes is not None:
            fitted_output = _fit_video_to_limit(output, max_bytes, settings)
        yield PreparedAttachment(fitted_output or output, "video/mp4", name)
    finally:
        output.unlink(missing_ok=True)
        if fitted_output is not None and fitted_output != output:
            fitted_output.unlink(missing_ok=True)


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

    def search_instagram_audio(
        self, account_id: str, *, audio_type: str = "music", query: str | None = None
    ) -> dict[str, Any]:
        if not account_id:
            raise ProviderError("Instagram account ID is missing")
        if audio_type not in {"music", "original_sound"}:
            raise ProviderError("Instagram audio type is invalid")
        params: dict[str, str] = {"audioType": audio_type}
        if query and query.strip():
            params["q"] = query.strip()
        return self._request("GET", f"/accounts/{account_id}/instagram/audio", params=params)

    def publish(self, delivery: Delivery, post: Post, connection: Connection) -> PublishResult:
        media = []
        for item in post_media_attachments(post):
            with prepared_attachment(item, self.settings) as prepared:
                media.append(self.upload_media(prepared))
        platform: dict[str, Any] = {"platform": connection.platform, "accountId": connection.external_id}
        platform_options = dict(delivery.platform_options or {})
        # A terminal Zernio failure can be explicitly retried by the user. In
        # that case the API stores a one-use request id internally so that the
        # corrected media is submitted as a new post instead of returning the
        # already failed Zernio post for the old idempotency key.
        request_id = delivery.id
        retry_request_id = (delivery.provider_response or {}).get("_retry_request_id")
        if isinstance(retry_request_id, str) and retry_request_id:
            request_id = retry_request_id
        # TikTok settings are a documented top-level exception. Passing a
        # nested `tiktokSettings` in platformSpecificData is ignored by Zernio
        # and risks confusing future API versions.
        payload: dict[str, Any] = {
            "content": post.content,
            "platforms": [platform],
            "publishNow": True,
        }
        if connection.platform == "tiktok":
            raw_tiktok_settings = platform_options.get("tiktokSettings")
            if not isinstance(raw_tiktok_settings, dict):
                raise ProviderError("TikTok publication needs confirmed TikTok settings")
            tiktok_settings = dict(raw_tiktok_settings)
            # TikTok drafts require an inbox editing flow and do not produce a
            # completed publication. The product intentionally supports only
            # final publishing, so legacy queued rows must not retain it.
            tiktok_settings.pop("draft", None)
            cover_attachment_id = tiktok_settings.pop("video_cover_attachment_id", None)
            if isinstance(cover_attachment_id, str) and cover_attachment_id:
                cover = post_cover_attachment(post, cover_attachment_id)
                with prepared_attachment(cover, self.settings) as prepared_cover:
                    tiktok_settings["video_cover_image_url"] = self.upload_media(prepared_cover)["url"]
            payload["tiktokSettings"] = tiktok_settings
        if connection.platform == "instagram":
            cover_attachment_id = platform_options.pop("instagramThumbnailAttachmentId", None)
            if isinstance(cover_attachment_id, str) and cover_attachment_id:
                cover = post_cover_attachment(post, cover_attachment_id)
                with prepared_attachment(cover, self.settings) as prepared_cover:
                    platform_options["instagramThumbnail"] = self.upload_media(prepared_cover)["url"]
        platform_specific_data = {
            key: value for key, value in platform_options.items() if key != "tiktokSettings"
        }
        if platform_specific_data:
            platform["platformSpecificData"] = platform_specific_data
        if media:
            payload["mediaItems"] = media
        response = self._request(
            "POST",
            "/posts",
            json=payload,
            headers={**self.headers, "x-request-id": request_id},
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
_LOCAL_TELEGRAM_BOT_SESSIONS: set[str] = set()
_TELEGRAM_CLOUD_API_BASE_URL = "https://api.telegram.org"


def ensure_local_telegram_bot_session(settings: Settings, bot_token: str) -> None:
    """Move a bot from Telegram's cloud endpoint to a configured local API once.

    Telegram requires `logOut` on the cloud Bot API before a bot token can be
    served by a local Bot API instance. Keep only a SHA-256 fingerprint in the
    process cache and never expose the token in any error.
    """
    local_base = str(settings.telegram_api_base_url).rstrip("/")
    if local_base == _TELEGRAM_CLOUD_API_BASE_URL:
        return
    token_fingerprint = hashlib.sha256(bot_token.encode()).hexdigest()
    if token_fingerprint in _LOCAL_TELEGRAM_BOT_SESSIONS:
        return
    try:
        with httpx.Client(timeout=20) as client:
            response = client.post(f"{_TELEGRAM_CLOUD_API_BASE_URL}/bot{bot_token}/logOut")
    except httpx.HTTPError as error:
        raise ProviderError(
            f"Telegram cloud logout failed: {_safe_error(error, bot_token)}", retryable=True
        ) from error
    _response_json(response, bot_token)
    _LOCAL_TELEGRAM_BOT_SESSIONS.add(token_fingerprint)


def list_telegram_targets(settings: Settings, bot_token: str) -> list[dict[str, str]]:
    """Return group/channel chats visible to a bot through pending Bot API updates.

    Telegram does not provide a Bot API method that lists every chat a bot has
    been added to.  A group command, a channel post, or a membership update is
    therefore used as an explicit, safe discovery signal.
    """
    if not bot_token:
        raise ProviderError("Telegram bot token is missing for this customer")
    ensure_local_telegram_bot_session(settings, bot_token)
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


def _telegram_album_kind(content_type: str) -> str:
    """Return the Telegram media-group family compatible with a file type."""
    if content_type.startswith(("image/", "video/")):
        return "visual"
    return "document"


def _telegram_attachment_batches(attachments: list[Attachment]) -> Iterator[list[Attachment]]:
    """Split media into compatible Telegram albums of no more than ten items."""
    batch: list[Attachment] = []
    kind: str | None = None
    for attachment in attachments:
        attachment_kind = _telegram_album_kind(attachment.content_type)
        if batch and (attachment_kind != kind or len(batch) == 10):
            yield batch
            batch = []
        batch.append(attachment)
        kind = attachment_kind
    if batch:
        yield batch


def _telegram_media_type(content_type: str) -> str:
    if content_type.startswith("image/"):
        return "photo"
    if content_type.startswith("video/"):
        return "video"
    return "document"


def publish_telegram(post: Post, connection: Connection, settings: Settings, bot_token: str) -> PublishResult:
    if not bot_token:
        raise ProviderError("Telegram bot token is missing for this customer")
    ensure_local_telegram_bot_session(settings, bot_token)
    base = str(settings.telegram_api_base_url).rstrip("/")
    result: list[dict[str, Any]] = []
    attachments = post_media_attachments(post)
    try:
        with httpx.Client(timeout=180) as client:
            if not attachments:
                response = client.post(
                    f"{base}/bot{bot_token}/sendMessage",
                    json={"chat_id": connection.external_id, "text": post.content},
                )
                result.append(_response_json(response, bot_token))
            else:
                caption_sent = False
                for batch in _telegram_attachment_batches(attachments):
                    # Telegram's sendMediaGroup requires 2–10 compatible
                    # items. A single trailing file remains a normal message.
                    if len(batch) == 1:
                        attachment = batch[0]
                        with prepared_attachment(attachment, settings) as prepared:
                            if prepared.path.stat().st_size > settings.telegram_max_upload_bytes:
                                raise ProviderError("Telegram attachment exceeds configured upload limit")
                            method, field = _telegram_method(prepared.content_type)
                            data = {"chat_id": connection.external_id}
                            if not caption_sent and post.content:
                                data["caption"] = post.content
                                caption_sent = True
                            with prepared.path.open("rb") as file_handle:
                                response = client.post(
                                    f"{base}/bot{bot_token}/{method}",
                                    data=data,
                                    files={field: (prepared.original_name, file_handle, prepared.content_type)},
                                )
                            result.append(_response_json(response, bot_token))
                        continue

                    with ExitStack() as stack:
                        media: list[dict[str, str]] = []
                        files: dict[str, tuple[str, Any, str]] = {}
                        for index, attachment in enumerate(batch):
                            prepared = stack.enter_context(prepared_attachment(attachment, settings))
                            if prepared.path.stat().st_size > settings.telegram_max_upload_bytes:
                                raise ProviderError("Telegram attachment exceeds configured upload limit")
                            field = f"file{index}"
                            item = {"type": _telegram_media_type(prepared.content_type), "media": f"attach://{field}"}
                            if not caption_sent and index == 0 and post.content:
                                item["caption"] = post.content
                                caption_sent = True
                            media.append(item)
                            files[field] = (
                                prepared.original_name,
                                stack.enter_context(prepared.path.open("rb")),
                                prepared.content_type,
                            )
                        response = client.post(
                            f"{base}/bot{bot_token}/sendMediaGroup",
                            data={"chat_id": connection.external_id, "media": json.dumps(media, ensure_ascii=False)},
                            files=files,
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


def _whapi_attachment_limit_bytes(attachment: PreparedAttachment, settings: Settings) -> int:
    if attachment.content_type == "application/pdf":
        return settings.whapi_max_document_bytes
    return settings.whapi_max_media_bytes


def _check_whapi_attachment_size(attachment: PreparedAttachment, settings: Settings) -> None:
    if attachment.path.stat().st_size > _whapi_attachment_limit_bytes(attachment, settings):
        raise ProviderError("attachment exceeds configured WhatsApp media limit")


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
    attachments = post_media_attachments(post)
    try:
        # The media endpoints accept multipart/form-data. Streaming the opened
        # file avoids Base64's 33% overhead and avoids reading multi-gigabyte
        # documents into worker memory.
        timeout = httpx.Timeout(settings.whapi_media_upload_timeout_seconds, connect=30)
        with httpx.Client(timeout=timeout) as client:
            if not attachments:
                response = client.post(
                    "https://gate.whapi.cloud/messages/text",
                    headers=headers,
                    json={"to": connection.external_id, "body": post.content},
                )
                result.append(_response_json(response, api_token))
            else:
                for index, attachment in enumerate(attachments):
                    video_limit = (
                        settings.whapi_max_media_bytes
                        if attachment.content_type.startswith("video/")
                        else None
                    )
                    with prepared_attachment(attachment, settings, max_bytes=video_limit) as prepared:
                        _check_whapi_attachment_size(prepared, settings)
                        payload = {
                            "to": connection.external_id,
                        }
                        if index == 0 and post.content:
                            payload["caption"] = post.content
                        if prepared.content_type == "application/pdf":
                            payload["filename"] = prepared.original_name
                        with prepared.path.open("rb") as media_file:
                            response = client.post(
                                f"https://gate.whapi.cloud/messages/{_whapi_endpoint(prepared.content_type)}",
                                headers=headers,
                                data=payload,
                                files={"media": (prepared.original_name, media_file, prepared.content_type)},
                            )
                        result.append(_response_json(response, api_token))
    except httpx.HTTPError as error:
        raise ProviderError(f"Whapi network error: {_safe_error(error, api_token)}") from error
    return PublishResult({"messages": result})
