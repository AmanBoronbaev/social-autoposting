import httpx

from app.models import Attachment, Connection, Post
from app.providers import (
    TELEGRAM_DISCOVERY_UPDATE_TYPES,
    WhapiClient,
    ZernioClient,
    list_telegram_targets,
    publish_whapi,
)
from app.settings import get_settings


class FakeClient:
    responses: dict[str, httpx.Response] = {}
    requests: list[tuple[str, str, dict[str, object]]] = []

    def __init__(self, *, timeout: int) -> None:
        del timeout

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        self.requests.append(("GET", url, kwargs))
        return self.responses[url]

    def post(self, url: str, **kwargs: object) -> httpx.Response:
        self.requests.append(("POST", url, kwargs))
        return self.responses[url]


def test_telegram_discovery_uses_group_and_channel_updates(monkeypatch) -> None:
    token = "test-telegram-token"
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    FakeClient.responses = {
        url: httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {"message": {"chat": {"id": -1001, "type": "supergroup", "title": "Team"}}},
                    {"channel_post": {"chat": {"id": -1002, "type": "channel", "title": "News"}}},
                    {
                        "message": {
                            "chat": {"id": 10, "type": "private", "first_name": "Owner"},
                            "forward_origin": {
                                "type": "channel",
                                "chat": {"id": -1003, "type": "channel", "title": "Forwarded News"},
                            },
                        }
                    },
                    {"message": {"chat": {"id": 10, "type": "private", "first_name": "Ignored"}}},
                ],
            },
        )
    }
    FakeClient.requests = []
    monkeypatch.setattr("app.providers.httpx.Client", FakeClient)

    targets = list_telegram_targets(get_settings(), token)

    assert targets == [
        {"id": "-1003", "kind": "channel", "label": "Forwarded News"},
        {"id": "-1002", "kind": "channel", "label": "News"},
        {"id": "-1001", "kind": "group", "label": "Team"},
    ]
    assert FakeClient.requests == [
        ("POST", url, {"json": {"timeout": 0, "allowed_updates": list(TELEGRAM_DISCOVERY_UPDATE_TYPES)}})
    ]


def test_whapi_discovery_returns_groups_and_channels(monkeypatch) -> None:
    groups_url = "https://gate.whapi.cloud/groups"
    channels_url = "https://gate.whapi.cloud/newsletters"
    FakeClient.responses = {
        groups_url: httpx.Response(200, json={"groups": [{"id": "111@g.us", "name": "Clients"}]}),
        channels_url: httpx.Response(200, json={"newsletters": [{"id": "222@newsletter", "name": "Company"}]}),
    }
    FakeClient.requests = []
    monkeypatch.setattr("app.providers.httpx.Client", FakeClient)

    targets = WhapiClient("test-whapi-token").list_targets()

    assert targets == [
        {"id": "222@newsletter", "kind": "channel", "label": "Company"},
        {"id": "111@g.us", "kind": "group", "label": "Clients"},
    ]
    assert FakeClient.requests == [
        ("GET", groups_url, {"headers": {"Authorization": "Bearer test-whapi-token"}}),
        ("GET", channels_url, {"headers": {"Authorization": "Bearer test-whapi-token"}}),
    ]


def test_whapi_sends_local_media_as_base64_not_a_public_link(monkeypatch, tmp_path) -> None:
    media_file = tmp_path / "picture.png"
    media_file.write_bytes(b"png bytes")
    attachment = Attachment(
        original_name="picture.png",
        content_type="image/png",
        size_bytes=media_file.stat().st_size,
        storage_key="unused-in-this-test",
    )
    post = Post(content="Caption")
    post.attachments.append(attachment)
    connection = Connection(external_id="123@g.us")
    endpoint = "https://gate.whapi.cloud/messages/image"
    FakeClient.responses = {endpoint: httpx.Response(200, json={"sent": True})}
    FakeClient.requests = []
    monkeypatch.setattr("app.providers.httpx.Client", FakeClient)
    monkeypatch.setattr("app.providers.attachment_path", lambda _attachment, _settings: media_file)

    response = publish_whapi(post, connection, get_settings(), "test-whapi-token")

    assert response.payload == {"messages": [{"sent": True}]}
    method, url, request = FakeClient.requests[0]
    assert (method, url) == ("POST", endpoint)
    assert request["json"] == {
        "to": "123@g.us",
        "caption": "Caption",
        "media": "data:image/png;name=picture.png;base64,cG5nIGJ5dGVz",
    }


def test_instagram_audio_search_uses_account_scoped_catalog(monkeypatch) -> None:
    client = ZernioClient(get_settings(), "test-token")
    calls: list[tuple[str, str, dict[str, object]]] = []

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        calls.append((method, path, kwargs))
        return {"audio": []}

    monkeypatch.setattr(client, "_request", fake_request)

    assert client.search_instagram_audio("instagram-account", audio_type="music", query="track") == {"audio": []}
    assert calls == [
        (
            "GET",
            "/accounts/instagram-account/instagram/audio",
            {"params": {"audioType": "music", "q": "track"}},
        )
    ]
