"""
Консьюмер для обработки событий платежей из RabbitMQ.

Обрабатывает:
1. События создания платежей (payments.new)
2. Мертвые письма из DLQ (payments.new.dlq) для мониторинга и алертинга
"""

import logging

from faststream.rabbit import RabbitRouter

from app.messaging.broker import (
    DEAD_EXCHANGE,
    DEAD_QUEUE,
    DEAD_ROUTING_KEY,
    PAYMENTS_EXCHANGE,
    PAYMENTS_QUEUE,
    PAYMENTS_ROUTING_KEY,
)
from app.messaging.schemas import PaymentCreatedEvent

logger: logging.Logger = logging.getLogger(__name__)

router: RabbitRouter = RabbitRouter()


@router.subscriber(
    queue=PAYMENTS_QUEUE,
    exchange=PAYMENTS_EXCHANGE,
    routing_key=PAYMENTS_ROUTING_KEY,
)
async def process_payment_created(event: PaymentCreatedEvent) -> None:
    """Обрабатывает событие создания платежа."""
    logger.info(f"Received payment.created: {event.payment_id}")


@router.subscriber(
    queue=DEAD_QUEUE,
    exchange=DEAD_EXCHANGE,
    routing_key=DEAD_ROUTING_KEY,
)
async def process_dead_event(event: dict[str, object]) -> None:
    """Принимает сообщения, угодившие в DLQ. Сигнал для алертинга."""
    logger.error(f"Message sent to DLQ: {event}")
