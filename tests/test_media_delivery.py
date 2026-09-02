import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import connection_dict, normalized_upload_content_type, post_dict, tiktok_photo_title_limit_error
from app.models import Attachment, Connection, Delivery, Post, User
from app.providers import ZernioClient, prepared_attachment, publish_telegram, publish_whapi
from app.settings import get_settings
from app.worker import recover_zernio_status_checks, zernio_failure_reason


def make_attachment(tmp_path: Path, *, content_type: str, name: str, content: bytes) -> Attachment:
    storage_key = "customer/upload"
    source = tmp_path / storage_key
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    return Attachment(
        storage_key=storage_key,
        original_name=name,
        content_type=content_type,
        size_bytes=len(content),
    )


class RecordingClient:
    response = httpx.Response(200, json={"sent": True})
    calls: list[dict[str, object]] = []

    def __init__(self, *, timeout: int) -> None:
        del timeout

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        return self.response


def test_whapi_uses_base64_json_media_not_an_external_url(tmp_path: Path, monkeypatch) -> None:
    settings = get_settings().model_copy(update={"media_dir": tmp_path})
    attachment = make_attachment(tmp_path, content_type="image/png", name="poster.png", content=b"hello")
    post = Post(content="Caption", attachments=[attachment])
    connection = Connection(external_id="123@g.us")
    RecordingClient.calls = []
    monkeypatch.setattr("app.providers.httpx.Client", RecordingClient)

    publish_whapi(post, connection, settings, "test-whapi-token")

    call = RecordingClient.calls[0]
    assert call["url"] == "https://gate.whapi.cloud/messages/image"
    assert call["json"] == {
        "to": "123@g.us",
        "caption": "Caption",
        "media": "data:image/png;name=poster.png;base64,aGVsbG8=",
    }
    assert "files" not in call


def test_telegram_sends_selected_images_as_ordered_albums(tmp_path: Path, monkeypatch) -> None:
    settings = get_settings().model_copy(update={"media_dir": tmp_path})
    # Deliberately create the Python list out of order. The persisted position,
    # not a database row order, defines which photo is first in Telegram.
    attachments: list[Attachment] = []
    for position in range(16):
        storage_key = f"customer/{position}.png"
        source = tmp_path / storage_key
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(f"image-{position}".encode())
        attachments.append(
            Attachment(
                position=position,
                storage_key=storage_key,
                original_name=f"{position}.png",
                content_type="image/png",
                size_bytes=source.stat().st_size,
            )
        )
    post = Post(content="Album caption", attachments=list(reversed(attachments)))
    connection = Connection(external_id="@client_channel")
    RecordingClient.calls = []
    monkeypatch.setattr("app.providers.httpx.Client", RecordingClient)

    publish_telegram(post, connection, settings, "test-telegram-token")

    assert len(RecordingClient.calls) == 2
    first, second = RecordingClient.calls
    assert str(first["url"]).endswith("/sendMediaGroup")
    assert str(second["url"]).endswith("/sendMediaGroup")
    first_media = json.loads(first["data"]["media"])
    second_media = json.loads(second["data"]["media"])
    assert [item["media"] for item in first_media] == [f"attach://file{index}" for index in range(10)]
    assert [item["media"] for item in second_media] == [f"attach://file{index}" for index in range(6)]
    assert first_media[0]["caption"] == "Album caption"
    assert "caption" not in second_media[0]
    assert first["files"]["file0"][0] == "0.png"
    assert second["files"]["file0"][0] == "10.png"


def test_video_is_prepared_as_mp4_before_delivery(tmp_path: Path, monkeypatch) -> None:
    settings = get_settings().model_copy(update={"media_dir": tmp_path})
    attachment = make_attachment(tmp_path, content_type="video/quicktime", name="camera.mov", content=b"source")

    monkeypatch.setattr("app.providers.shutil.which", lambda _: "/usr/bin/ffmpeg")

    captured: list[str] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        captured.extend(command)
        Path(command[-1]).write_bytes(b"prepared")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr("app.providers.subprocess.run", fake_run)

    with prepared_attachment(attachment, settings) as prepared:
        output = prepared.path
        assert prepared.content_type == "video/mp4"
        assert prepared.original_name == "camera.mp4"
        assert output.is_file()
    assert ["-vf", "fps=30"] == captured[captured.index("-vf") : captured.index("-vf") + 2]
    assert not output.exists()


def test_history_contains_destination_and_scoped_media_preview_url(tmp_path: Path) -> None:
    settings = get_settings().model_copy(
        update={"media_dir": tmp_path, "admin_path": "/panel-0123456789abcdef/"}
    )
    attachment = make_attachment(tmp_path, content_type="image/png", name="poster.png", content=b"preview")
    attachment.id = "attachment-1"
    connection = Connection(label="Client channel", provider="telegram", platform="telegram")
    delivery = Delivery(connection=connection, status="failed", attempts=2, error="Zernio finished with status: failed")
    post = Post(content="Full post text", attachments=[attachment], deliveries=[delivery])

    result = post_dict(post, settings)

    assert result["attachments"][0]["media_url"].startswith("/panel-0123456789abcdef/v1/media/attachment-1?token=")
    assert result["deliveries"][0]["destination"] == {
        "label": "Client channel",
        "platform": "telegram",
    }
    assert result["deliveries"][0]["error"] == (
        "Публикация не выполнена. Проверьте требования выбранной площадки и повторите попытку."
    )
    assert "platform_options" not in result["deliveries"][0]

    internal = post_dict(post, settings, include_internal_details=True)
    assert internal["deliveries"][0]["destination"]["provider"] == "telegram"
    assert internal["deliveries"][0]["error"] == "Zernio finished with status: failed"


def test_customer_history_translates_frame_rate_error_without_provider_name() -> None:
    delivery = Delivery(status="failed", error="Zernio finished: Video frame rate is not supported")
    post = Post(deliveries=[delivery])

    result = post_dict(post)

    assert result["deliveries"][0]["error"] == "Видео должно иметь частоту от 23 до 60 кадров в секунду."


def test_customer_connection_does_not_receive_provider_name() -> None:
    connection = Connection(provider="zernio", platform="tiktok", label="TikTok", external_id="account")

    public = connection_dict(connection, include_provider=False)

    assert "provider" not in public
    assert public["platform"] == "tiktok"


def test_common_video_extension_is_accepted_when_browser_reports_generic_binary() -> None:
    assert normalized_upload_content_type("application/octet-stream", "camera.MKV") == "video/x-matroska"
    assert normalized_upload_content_type("application/octet-stream", "unknown.bin") is None


def test_safari_camera_image_extension_is_accepted_when_mime_type_is_missing() -> None:
    assert normalized_upload_content_type("application/octet-stream", "IMG_1234.HEIC") == "image/heic"


def test_tiktok_photo_title_limit_is_checked_before_delivery() -> None:
    assert tiktok_photo_title_limit_error("x" * 90) is None
    assert tiktok_photo_title_limit_error("x" * 91) == (
        "Для фотокарусели TikTok текст может содержать до 90 символов. Сейчас: 91. "
        "Сократите текст или публикуйте как видео."
    )


def test_zernio_uses_a_cover_only_as_cover_not_as_post_media(tmp_path: Path, monkeypatch) -> None:
    settings = get_settings().model_copy(update={"media_dir": tmp_path})
    media = make_attachment(tmp_path, content_type="image/png", name="post.png", content=b"post")
    media.id = "media"
    cover = make_attachment(tmp_path, content_type="image/png", name="cover.png", content=b"cover")
    cover.id = "cover"
    cover.storage_key = "customer/cover"
    (tmp_path / cover.storage_key).write_bytes(b"cover")
    cover.role = "cover"
    post = Post(content="Caption", attachments=[media, cover])
    connection = Connection(platform="instagram", external_id="instagram-account")
    delivery = Delivery(
        platform_options={
            "instagramThumbnailAttachmentId": "cover",
            "audioConfiguration": {"audioId": "audio-1", "audioVolume": 70, "videoVolume": 80},
        }
    )
    client = ZernioClient(settings, "test-token")
    requests: list[dict[str, object]] = []

    def fake_upload(prepared):
        return {"url": f"https://media.example/{prepared.original_name}", "type": "image"}

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        requests.append({"method": method, "path": path, **kwargs})
        return {"post": {"id": "published"}}

    monkeypatch.setattr(client, "upload_media", fake_upload)
    monkeypatch.setattr(client, "_request", fake_request)

    client.publish(delivery, post, connection)

    payload = requests[0]["json"]
    assert isinstance(payload, dict)
    assert payload["mediaItems"] == [{"url": "https://media.example/post.png", "type": "image"}]
    platform_data = payload["platforms"][0]["platformSpecificData"]
    assert platform_data["instagramThumbnail"] == "https://media.example/cover.png"
    assert platform_data["audioConfiguration"]["audioId"] == "audio-1"
    assert "instagramThumbnailAttachmentId" not in platform_data


def test_zernio_tiktok_custom_cover_is_sent_as_a_public_image_url(tmp_path: Path, monkeypatch) -> None:
    settings = get_settings().model_copy(update={"media_dir": tmp_path})
    media = make_attachment(tmp_path, content_type="image/png", name="post.png", content=b"post")
    media.id = "media"
    cover = make_attachment(tmp_path, content_type="image/webp", name="cover.webp", content=b"cover")
    cover.id = "cover"
    cover.storage_key = "customer/cover"
    (tmp_path / cover.storage_key).write_bytes(b"cover")
    cover.role = "cover"
    post = Post(content="Caption", attachments=[media, cover])
    connection = Connection(platform="tiktok", external_id="tiktok-account")
    delivery = Delivery(
        platform_options={
            "tiktokSettings": {
                "privacy_level": "PUBLIC_TO_EVERYONE",
                "content_preview_confirmed": True,
                "express_consent_given": True,
                "video_cover_attachment_id": "cover",
            }
        }
    )
    client = ZernioClient(settings, "test-token")
    requests: list[dict[str, object]] = []

    def fake_upload(prepared):
        return {"url": f"https://media.example/{prepared.original_name}", "type": "image"}

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        requests.append({"method": method, "path": path, **kwargs})
        return {"post": {"id": "published"}}

    monkeypatch.setattr(client, "upload_media", fake_upload)
    monkeypatch.setattr(client, "_request", fake_request)

    client.publish(delivery, post, connection)

    payload = requests[0]["json"]
    assert isinstance(payload, dict)
    settings_payload = payload["tiktokSettings"]
    assert settings_payload["video_cover_image_url"] == "https://media.example/cover.webp"
    assert "video_cover_attachment_id" not in settings_payload


def test_recovery_resumes_only_an_already_accepted_zernio_delivery(tmp_path: Path, monkeypatch) -> None:
    from app.database import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'worker.sqlite'}")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    session = TestSession()
    user = User(email="owner@example.test", password_hash="hash")
    session.add(user)
    session.flush()
    connection = Connection(user_id=user.id, provider="zernio", platform="instagram", label="IG", external_id="acc")
    post = Post(user_id=user.id, content="Test", scheduled_at=datetime.now(UTC))
    session.add_all([connection, post])
    session.flush()
    accepted = Delivery(
        post_id=post.id,
        connection_id=connection.id,
        status="processing",
        available_at=datetime.now(UTC),
        provider_response={"post": {"id": "zernio-post"}},
    )
    unfinished = Delivery(
        post_id=post.id,
        connection_id=connection.id,
        status="processing",
        available_at=datetime.now(UTC),
    )
    session.add_all([accepted, unfinished])
    session.commit()

    monkeypatch.setattr("app.worker.SessionLocal", TestSession)

    assert recover_zernio_status_checks() == 1
    session.expire_all()
    assert session.get(Delivery, accepted.id).status == "provider_processing"
    assert session.get(Delivery, unfinished.id).status == "processing"


def test_zernio_failure_reason_supports_nested_platform_error() -> None:
    assert zernio_failure_reason(
        {"post": {"platformResults": [{"status": "failed", "error": {"message": "TikTok rejected the media"}}]}}
    ) == "TikTok rejected the media"
