import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database import SessionLocal
from app.models import Connection, Delivery, Post, ProviderCredential
from app.providers import ProviderError, PublishResult, ZernioClient, publish_telegram, publish_whapi
from app.security import decrypt_credentials
from app.settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
# httpx logs complete request URLs at INFO level. Telegram bot tokens and signed
# upload links may be part of those URLs, so keep transport logging out of logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def now() -> datetime:
    return datetime.now(UTC)


def claim_delivery(session: Session) -> str | None:
    # Never blindly retry a request after the process crashed: Telegram/Whapi
    # might have accepted it just before the crash, causing a duplicate post.
    # Flag abandoned work as unknown and require a human to retry it explicitly.
    stale = now() - timedelta(minutes=10)
    abandoned = session.scalar(
        select(Delivery)
        .where(Delivery.status == "processing", Delivery.locked_at < stale)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if abandoned is not None:
        abandoned.status = "unknown"
        abandoned.error = "Worker stopped during this delivery. Check the destination before retrying."
        abandoned.locked_at = None
        session.commit()
        return None
    candidate = session.scalar(
        select(Delivery)
        .join(Delivery.post)
        .where(
            Post.scheduled_at <= now(),
            Delivery.available_at <= now(),
            Delivery.status.in_(("queued", "provider_processing")),
        )
        .order_by(Delivery.available_at, Delivery.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if candidate is None:
        return None
    was_provider_processing = candidate.status == "provider_processing"
    candidate.status = "processing"
    candidate.locked_at = now()
    if not was_provider_processing:
        candidate.attempts += 1
    session.commit()
    return candidate.id


def process_delivery(delivery_id: str) -> None:
    settings = get_settings()
    with SessionLocal() as session:
        delivery = session.scalar(
            select(Delivery)
            .options(
                selectinload(Delivery.post).selectinload(Post.attachments),
                selectinload(Delivery.connection),
            )
            .where(Delivery.id == delivery_id)
        )
        if delivery is None or delivery.status != "processing":
            return
        connection: Connection = delivery.connection
        try:
            credential = session.scalar(
                select(ProviderCredential).where(
                    ProviderCredential.user_id == delivery.post.user_id,
                    ProviderCredential.provider == connection.provider,
                )
            )
            if credential is None:
                raise ProviderError("The administrator has not added this customer's provider token")
            try:
                api_token = decrypt_credentials(credential.encrypted_credentials, settings).get("api_token", "")
            except ValueError as error:
                raise ProviderError("Stored provider token cannot be decrypted") from error
            if connection.provider == "zernio":
                zernio = ZernioClient(settings, api_token)
                existing_post_id = zernio_post_id(delivery.provider_response)
                result = (
                    PublishResult(zernio.get_post(existing_post_id))
                    if existing_post_id
                    else zernio.publish(delivery, delivery.post, connection)
                )
            elif connection.provider == "telegram":
                result = publish_telegram(delivery.post, connection, settings, api_token)
            elif connection.provider == "whapi":
                result = publish_whapi(delivery.post, connection, settings, api_token)
            else:
                raise ProviderError(f"unsupported provider: {connection.provider}")
        except ProviderError as error:
            if connection.provider == "zernio" and zernio_post_id(delivery.provider_response):
                delivery.status = "provider_processing"
                delivery.available_at = now() + timedelta(seconds=30)
                delivery.error = str(error)[:2000]
                delivery.locked_at = None
                session.commit()
                logger.warning("delivery %s status check failed: %s", delivery.id, error)
                return
            # Only Zernio declares request idempotency. Retrying Telegram or
            # Whapi after a timeout can duplicate a user-facing post, so those
            # are deliberately left for an explicit user retry in a later UI.
            if connection.provider == "zernio" and error.retryable and delivery.attempts < 3:
                delivery.status = "queued"
                delivery.available_at = now() + timedelta(seconds=30 * (2 ** (delivery.attempts - 1)))
            else:
                delivery.status = "failed"
            delivery.error = str(error)[:2000]
            delivery.locked_at = None
            session.commit()
            logger.warning("delivery %s failed: %s", delivery.id, error)
            return
        if connection.provider == "zernio":
            zernio_status = zernio_post_status(result.payload)
            delivery.provider_response = result.payload
            if zernio_status not in {"published", "failed", "partial"}:
                delivery.status = "provider_processing"
                delivery.available_at = now() + timedelta(seconds=15)
                delivery.error = None
                delivery.locked_at = None
                session.commit()
                logger.info("delivery %s accepted by Zernio; awaiting final platform status", delivery.id)
                return
            if zernio_status in {"failed", "partial"}:
                delivery.status = "failed"
                delivery.error = f"Zernio finished with status: {zernio_status}"
                delivery.locked_at = None
                session.commit()
                logger.warning("delivery %s failed in Zernio: %s", delivery.id, zernio_status)
                return
        delivery.status = "published"
        delivery.provider_response = result.payload
        delivery.error = None
        delivery.completed_at = now()
        delivery.locked_at = None
        session.commit()
        logger.info("delivery %s published via %s", delivery.id, connection.provider)


def zernio_post_id(payload: dict | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    post = payload.get("post")
    if not isinstance(post, dict):
        return None
    post_id = post.get("_id") or post.get("id")
    return post_id if isinstance(post_id, str) else None


def zernio_post_status(payload: dict) -> str | None:
    post = payload.get("post")
    status = post.get("status") if isinstance(post, dict) else None
    return status if isinstance(status, str) else None


def recover_zernio_status_checks() -> int:
    """Resume only status checks left by a stopped worker, never a new publish.

    A Zernio response with a post ID proves that the original create request was
    accepted. It is therefore safe to turn an interrupted `processing` row back
    into `provider_processing`: the worker will call GET /posts/{id}, not POST
    /posts, so no duplicate social post can be produced.
    """
    with SessionLocal() as session:
        deliveries = list(
            session.scalars(
                select(Delivery)
                .join(Delivery.connection)
                .where(
                    Delivery.status == "processing",
                    Connection.provider == "zernio",
                    Delivery.provider_response.is_not(None),
                )
                .with_for_update(skip_locked=True)
            )
        )
        recovered = 0
        for delivery in deliveries:
            if zernio_post_id(delivery.provider_response) is None:
                continue
            delivery.status = "provider_processing"
            delivery.available_at = now()
            delivery.locked_at = None
            delivery.error = None
            recovered += 1
        if recovered:
            session.commit()
        return recovered


def main() -> None:
    settings = get_settings()
    recovered = recover_zernio_status_checks()
    if recovered:
        logger.info("resumed %s interrupted Zernio status check(s)", recovered)
    logger.info("worker started")
    while True:
        with SessionLocal() as session:
            delivery_id = claim_delivery(session)
        if delivery_id is not None:
            process_delivery(delivery_id)
        else:
            time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
