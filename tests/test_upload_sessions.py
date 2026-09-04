import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.main import (
    append_upload_chunk,
    complete_upload_session,
    create_upload_session,
    current_upload_session,
    upload_session_path,
)
from app.models import Attachment, UploadSession, User
from app.schemas import UploadSessionIn
from app.security import hash_password
from app.settings import get_settings


def chunk_request(body: bytes) -> Request:
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "PATCH", "headers": []}, receive)


def run(coroutine):
    return asyncio.run(coroutine)


def test_resumable_upload_continues_from_the_last_acknowledged_chunk(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'uploads.sqlite'}")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    settings = get_settings().model_copy(update={"media_dir": tmp_path / "media"})

    with TestSession() as db:
        user = User(email="mobile@example.test", password_hash=hash_password("password longer than twelve"))
        db.add(user)
        db.flush()
        started = create_upload_session(
            UploadSessionIn(
                original_name="Safari camera.MOV",
                content_type="application/octet-stream",
                size_bytes=7,
            ),
            user,
            db,
            settings,
        )
        db.commit()

        first = run(
            append_upload_chunk(
                started["id"],
                chunk_request(b"abc"),
                0,
                user,
                db,
                settings,
            )
        )
        db.commit()
        assert first["offset"] == 3
        session = current_upload_session(started["id"], user.id, db)
        assert upload_session_path(session, settings).read_bytes() == b"abc"

        with pytest.raises(HTTPException, match="offset") as error:
            run(
                append_upload_chunk(
                    started["id"],
                    chunk_request(b"defg"),
                    0,
                    user,
                    db,
                    settings,
                )
            )
        assert error.value.status_code == 409

        second = run(
            append_upload_chunk(
                started["id"],
                chunk_request(b"defg"),
                3,
                user,
                db,
                settings,
            )
        )
        db.commit()
        assert second["offset"] == 7

        completed = complete_upload_session(started["id"], user, db, settings)
        db.commit()
        attachment = db.get(Attachment, completed["id"])
        assert attachment is not None
        assert (settings.media_dir / attachment.storage_key).read_bytes() == b"abcdefg"
        assert db.get(UploadSession, started["id"]) is None

    engine.dispose()
