import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def uuid4() -> str:
    return str(uuid.uuid4())


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class User(Timestamped, Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    display_name: Mapped[str] = mapped_column(String(160), default="")
    is_superuser: Mapped[bool] = mapped_column(default=False, index=True)
    is_active: Mapped[bool] = mapped_column(default=True, index=True)

    connections: Mapped[list["Connection"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    credentials: Mapped[list["ProviderCredential"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    posts: Mapped[list["Post"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    attachments: Mapped[list["Attachment"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Connection(Timestamped, Base):
    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("user_id", "provider", "external_id", name="uq_connection_owner"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # zernio, telegram or whapi
    provider: Mapped[str] = mapped_column(String(32), index=True)
    # instagram, tiktok, telegram, whatsapp
    platform: Mapped[str] = mapped_column(String(32), index=True)
    label: Mapped[str] = mapped_column(String(160))
    external_id: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="connected")

    user: Mapped[User] = relationship(back_populates="connections")
    deliveries: Mapped[list["Delivery"]] = relationship(back_populates="connection")


class ProviderCredential(Timestamped, Base):
    __tablename__ = "provider_credentials"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_credential_owner_provider"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # zernio, telegram or whapi. The raw secret is always Fernet-encrypted.
    provider: Mapped[str] = mapped_column(String(32), index=True)
    encrypted_credentials: Mapped[str] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="credentials")


class Post(Timestamped, Base):
    __tablename__ = "posts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    user: Mapped[User] = relationship(back_populates="posts")
    # The selected media order is meaningful for albums and carousels: the
    # first item is the cover/first slide on most social platforms.
    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="post",
        order_by=lambda: (Attachment.position, Attachment.created_at, Attachment.id),
    )
    deliveries: Mapped[list["Delivery"]] = relationship(back_populates="post", cascade="all, delete-orphan")


class Attachment(Timestamped, Base):
    __tablename__ = "attachments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    post_id: Mapped[str | None] = mapped_column(ForeignKey("posts.id", ondelete="SET NULL"), index=True)
    # A cover is stored with its post but must never become a post media item.
    # Existing rows default to ``media`` during the additive database upgrade.
    role: Mapped[str] = mapped_column(String(16), default="media", server_default="media", index=True)
    # Position in the post selected by the customer. It is persisted instead
    # of relying on database row order, which is undefined for an SQL IN query.
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    original_name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)

    user: Mapped[User] = relationship(back_populates="attachments")
    post: Mapped[Post | None] = relationship(back_populates="attachments")


class UploadSession(Timestamped, Base):
    """Private, resumable staging area for one browser file upload."""

    __tablename__ = "upload_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    original_name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(255))
    total_bytes: Mapped[int] = mapped_column(BigInteger)
    uploaded_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Delivery(Timestamped, Base):
    __tablename__ = "deliveries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("connections.id", ondelete="RESTRICT"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    platform_options: Mapped[dict | None] = mapped_column(JSON)
    provider_response: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)

    post: Mapped[Post] = relationship(back_populates="deliveries")
    connection: Mapped[Connection] = relationship(back_populates="deliveries")
