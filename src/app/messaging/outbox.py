"""Outbox relay: надёжная публикация событий из PostgreSQL в RabbitMQ.

Событие и платёж создаются в одной транзакции. Relay публикует события с
подтверждением брокера и только после успешной публикации помечает строку
``published``. Это даёт at-least-once доставку: при падении между publish и
commit событие будет опубликовано повторно, а consumer идемпотентен.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.config import settings
from app.db.session import async_session
from app.messaging.broker import (
    PAYMENTS_EXCHANGE,
    PAYMENTS_ROUTING_KEY,
    broker,
)
from app.models.enums import OutboxStatusEnum
from app.models.outbox import Outbox

logger = logging.getLogger(__name__)


def outbox_backoff_seconds(attempts: int) -> float:
    """Экспоненциальная задержка публикации с верхней границей."""
    exponent = min(max(attempts, 1), 10)
    return min(settings.OUTBOX_MAX_BACKOFF_SECONDS, float(2**exponent))


async def publish_pending_events() -> int:
    """Опубликовать пачку неопубликованных событий.

    Возвращает число успешно опубликованных событий. Строки блокируются
    ``FOR UPDATE SKIP LOCKED``, поэтому несколько relay-процессов могут
    работать параллельно.
    """
    now = datetime.now(UTC)
    published = 0

    async with async_session() as session, session.begin():
        stmt = (
            select(Outbox)
            .where(
                Outbox.status == OutboxStatusEnum.PENDING,
                Outbox.next_attempt_at <= now,
            )
            .order_by(Outbox.created_at)
            .limit(settings.OUTBOX_BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
        events = list((await session.execute(stmt)).scalars().all())
        if not events:
            return 0

        for event in events:
            try:
                await broker.publish(
                    event.payload,
                    exchange=PAYMENTS_EXCHANGE,
                    routing_key=PAYMENTS_ROUTING_KEY,
                    headers={
                        "event_id": str(event.id),
                        "event_type": event.event_type,
                        "x-retry-count": 1,
                    },
                    message_id=str(event.id),
                    persist=True,
                )
            except Exception as exc:
                event.attempts += 1
                event.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                event.next_attempt_at = now + timedelta(
                    seconds=outbox_backoff_seconds(event.attempts)
                )
                logger.exception(
                    f"Outbox publish failed (event={event.id}, "
                    f"attempts={event.attempts})"
                )
                # Не блокируем остальные события на недоступном брокере.
                break

            event.attempts += 1
            event.status = OutboxStatusEnum.PUBLISHED
            event.published_at = datetime.now(UTC)
            event.last_error = None
            published += 1
            logger.info(f"Outbox event published: {event.id}")

    if published:
        logger.info(f"Published {published} outbox event(s)")
    return published
