"""
Outbox relay - фоновый процесс для надежной публикации событий в RabbitMQ.

Архитектура:
1. Периодически вычитывает неопубликованные события из таблицы outbox
2. Отправляет их в RabbitMQ
3. Помечает как опубликованные

Гарантии:
- At-least-once: если relay упадет после отправки, но до коммита статуса,
  событие будет отправлено повторно при следующем цикле
- Идемпотентность консьюмера обязательна (реализуется на Этапе 5)
"""

import logging

from sqlalchemy import select

from app.core.config import settings
from app.db.session import async_session
from app.messaging.broker import (
    PAYMENTS_EXCHANGE,
    PAYMENTS_QUEUE,
    PAYMENTS_ROUTING_KEY,
    broker,
)
from app.models.enums import OutboxStatusEnum
from app.models.outbox import Outbox

logger: logging.Logger = logging.getLogger(__name__)


async def publish_pending_events() -> int:
    """
    Забирает до N неопубликованных событий и отправляет их в RabbitMQ.
    Возвращает количество опубликованных событий.
    """
    async with async_session() as session, session.begin():
        stmt = (
            select(Outbox)
            .where(Outbox.status == OutboxStatusEnum.PENDING)
            .order_by(Outbox.created_at)
            .limit(settings.OUTBOX_BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(stmt)
        events: list[Outbox] = list(result.scalars().all())

        if not events:
            logger.info("Published outbox events not found")
            return 0

        for event in events:
            await broker.publish(
                event.payload,
                queue=PAYMENTS_QUEUE,
                exchange=PAYMENTS_EXCHANGE,
                routing_key=PAYMENTS_ROUTING_KEY,
                headers={
                    "event_id": str(event.id),
                    "event_type": event.event_type,
                },
            )
            event.status = OutboxStatusEnum.PUBLISHED

        events_count = len(events)

        logger.info(f"Published {events_count} outbox events")
        return events_count
