import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import normalized_upload_content_type, post_dict
from app.models import Attachment, Connection, Delivery, Post, User
from app.providers import prepared_attachment, publish_whapi
from app.settings import get_settings
from app.worker import recover_zernio_status_checks, zernio_failure_reason


def make_attachment(tmp_path: Path, *, content_type: str, name: str, content: bytes) -> Attachment:
    storage_key = "customer/upload"
    source = tmp_path / storage_key
    source.parent.mkdir(parents=True)
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
    delivery = Delivery(connection=connection, status="published", attempts=2)
    post = Post(content="Full post text", attachments=[attachment], deliveries=[delivery])

    result = post_dict(post, settings)

    assert result["attachments"][0]["media_url"].startswith("/panel-0123456789abcdef/v1/media/attachment-1?token=")
    assert result["deliveries"][0]["destination"] == {
        "label": "Client channel",
        "platform": "telegram",
        "provider": "telegram",
    }


def test_common_video_extension_is_accepted_when_browser_reports_generic_binary() -> None:
    assert normalized_upload_content_type("application/octet-stream", "camera.MKV") == "video/x-matroska"
    assert normalized_upload_content_type("application/octet-stream", "unknown.bin") is None


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
