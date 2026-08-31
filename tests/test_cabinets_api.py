import os
from collections.abc import AsyncIterator

import httpx
import pytest

from app.database import Base, engine
from app.main import app
from app.models import User
from app.security import hash_password


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as test_client:
        yield test_client
    Base.metadata.drop_all(engine)
    engine.dispose()
    try:
        os.unlink("/tmp/autoposting_platform_test.db")
    except FileNotFoundError:
        pass


async def owner_token(client: httpx.AsyncClient) -> str:
    from app.database import SessionLocal

    with SessionLocal.begin() as session:
        session.add(
            User(
                email="owner@example.com",
                password_hash=hash_password("owner password longer than twelve"),
                is_superuser=True,
            )
        )
    response = await client.post(
        "/v1/auth/login",
        json={"email": "owner@example.com", "password": "owner password longer than twelve"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.anyio
async def test_owner_creates_cabinet_and_user_schedules_a_post(client: httpx.AsyncClient) -> None:
    owner = await owner_token(client)
    created = await client.post(
        "/v1/admin/users",
        headers=auth(owner),
        json={
            "email": "client@example.com",
            "password": "client password longer than twelve",
            "display_name": "Client Cabinet",
        },
    )
    assert created.status_code == 201
    user_id = created.json()["id"]

    login = await client.post(
        "/v1/auth/login",
        json={"email": "client@example.com", "password": "client password longer than twelve"},
    )
    user_token = login.json()["access_token"]
    credential = await client.post(
        f"/v1/admin/users/{user_id}/credentials",
        headers=auth(owner),
        json={"provider": "telegram", "api_token": "123456:telegram-bot-token-for-client"},
    )
    assert credential.status_code == 200
    assert "api_token" not in credential.text
    assert (await client.get(f"/v1/admin/users/{user_id}/credentials", headers=auth(user_token))).status_code == 403
    destination = await client.post(
        f"/v1/admin/users/{user_id}/connections",
        headers=auth(owner),
        json={
            "provider": "telegram",
            "platform": "telegram",
            "label": "Client channel",
            "external_id": "@client_channel",
        },
    )
    assert destination.status_code == 201

    user_connections = await client.get("/v1/connections", headers=auth(user_token))
    assert [item["id"] for item in user_connections.json()] == [destination.json()["id"]]
    assert (await client.post("/v1/connections", headers=auth(user_token), json={})).status_code == 405

    post = await client.post(
        "/v1/posts",
        headers=auth(user_token),
        json={
            "content": "Scheduled post",
            "connection_ids": [destination.json()["id"]],
            "scheduled_at": "2030-01-02T03:04:05+00:00",
        },
    )
    assert post.status_code == 201
    assert post.json()["deliveries"][0]["status"] == "queued"

    users = await client.get("/v1/admin/users", headers=auth(owner))
    assert any(item["id"] == user_id for item in users.json())
    forbidden = await client.get("/v1/admin/users", headers=auth(user_token))
    assert forbidden.status_code == 403


@pytest.mark.anyio
async def test_owner_can_add_a_private_whapi_destination_without_reading_it_back(client: httpx.AsyncClient) -> None:
    owner = await owner_token(client)
    created = await client.post(
        "/v1/admin/users",
        headers=auth(owner),
        json={"email": "client@example.com", "password": "client password longer than twelve"},
    )
    secret = "a-whapi-token-that-must-never-appear-in-api-response"
    credential = await client.post(
        f"/v1/admin/users/{created.json()['id']}/credentials",
        headers=auth(owner),
        json={
            "provider": "whapi",
            "api_token": secret,
        },
    )
    assert credential.status_code == 200
    assert secret not in credential.text
    response = await client.post(
        f"/v1/admin/users/{created.json()['id']}/connections",
        headers=auth(owner),
        json={
            "provider": "whapi",
            "platform": "whatsapp",
            "label": "Client group",
            "external_id": "123456@g.us",
        },
    )
    assert response.status_code == 201
