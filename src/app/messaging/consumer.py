"""Consumer платёжных событий с broker-level retry и Dead Letter Queue."""

import asyncio
import logging
import random
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from aio_pika.abc import HeadersType
from faststream import AckPolicy
from faststream.rabbit import RabbitMessage, RabbitQueue
from sqlalchemy import select, update

from app.core.config import settings
from app.db.session import async_session
from app.messaging.broker import (
    PAYMENTS_CHANNEL,
    PAYMENTS_EXCHANGE,
    PAYMENTS_QUEUE,
    RETRY_QUEUES,
    broker,
)
from app.messaging.schemas import PaymentCreatedEvent
from app.models.enums import PaymentStatusEnum
from app.models.payment import Payment
from app.services.errors import PaymentNotFoundError

logger = logging.getLogger(__name__)


class WebhookDeliveryError(Exception):
    """Webhook не принят получателем."""


class RetryExhaustedError(Exception):
    """Исчерпаны все попытки обработки сообщения."""


def read_retry_count(headers: Mapping[str, Any] | None) -> int:
    """Номер текущей попытки из заголовка сообщения (по умолчанию 1)."""
    raw = (headers or {}).get("x-retry-count", 1)
    if isinstance(raw, bytes):
        raw = raw.decode(errors="ignore")
    try:
        attempt = int(raw)
    except TypeError:
        return 1
    except ValueError:
        return 1
    return max(1, attempt)


def retry_queue_for_attempt(attempt: int) -> RabbitQueue:
    """Retry-очередь с задержкой для указанной (следующей) попытки."""
    if not RETRY_QUEUES:
        raise RetryExhaustedError("Retry queues are not configured")
    index = min(max(attempt - 2, 0), len(RETRY_QUEUES) - 1)
    return RETRY_QUEUES[index]


def build_webhook_payload(payment: Payment) -> dict[str, Any]:
    """Тело webhook-уведомления. ``event_id`` стабилен между повторами."""
    if payment.status not in (
        PaymentStatusEnum.SUCCEEDED,
        PaymentStatusEnum.FAILED,
    ):
        raise ValueError("Webhook can only be sent for a finished payment")

    event_type = f"payment.{payment.status.value}"
    return {
        "event_id": f"{payment.id}:{payment.status.value}",
        "event": event_type,
        "payment_id": str(payment.id),
        "status": payment.status.value,
        "amount": format(payment.amount, ".2f"),
        "currency": payment.currency.value,
        "description": payment.description,
        "metadata": payment.metadata_ or {},
        "failure_reason": payment.failure_reason,
        "created_at": payment.created_at.isoformat(),
        "processed_at": (
            payment.processed_at.isoformat() if payment.processed_at else None
        ),
    }


def _log_truncated(response: httpx.Response) -> str:
    return response.text[:500]


async def _send_webhook(payment: Payment) -> None:
    payload = build_webhook_payload(payment)
    event_type = payload["event"]

    async with httpx.AsyncClient(timeout=settings.WEBHOOK_TIMEOUT_SECONDS) as client:
        response = await client.post(
            payment.webhook_url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "payment-service/1.0",
                "X-Event-Type": event_type,
                "X-Payment-Id": str(payment.id),
                "X-Webhook-Id": payload["event_id"],
            },
        )
        if not response.is_success:
            raise WebhookDeliveryError(
                f"Webhook returned HTTP {response.status_code}: "
                f"{_log_truncated(response)}"
            )

    logger.info(
        f"Webhook delivered: payment={payment.id} event={event_type} "
        f"status={response.status_code}"
    )


async def _simulate_gateway_call() -> bool:
    """Эмуляция внешнего шлюза: 2-5 секунд, 90% успешных ответов."""
    delay = random.uniform(
        settings.PROCESSING_MIN_SECONDS,
        settings.PROCESSING_MAX_SECONDS,
    )
    logger.info(f"Simulating payment gateway call for {delay:.2f}s")
    await asyncio.sleep(delay)
    return random.random() < settings.PAYMENT_SUCCESS_RATE


async def _process_payment(payment_id: UUID) -> None:
    """Обработать платёж и доставить webhook ровно один раз в рамках попытки."""
    async with async_session() as session:
        payment = (
            await session.execute(
                select(Payment).where(Payment.id == payment_id).with_for_update()
            )
        ).scalar_one_or_none()
        if payment is None:
            raise PaymentNotFoundError(f"Payment {payment_id} not found")

        if payment.status == PaymentStatusEnum.PENDING:
            succeeded = await _simulate_gateway_call()
            payment.status = (
                PaymentStatusEnum.SUCCEEDED if succeeded else PaymentStatusEnum.FAILED
            )
            payment.processed_at = datetime.now(UTC)
            payment.failure_reason = None if succeeded else "simulated_gateway_decline"
            # Фиксируем результат до webhook. Если webhook упадёт, повторная
            # попытка не будет заново «бросать кубик» и менять статус.
            await session.commit()
            logger.info(
                f"Payment {payment.id} processed with status {payment.status.value}"
            )

        if payment.webhook_delivered:
            logger.info(f"Payment {payment.id} webhook already delivered; skip")
            return

        payment.webhook_attempts += 1
        try:
            await _send_webhook(payment)
        except Exception as exc:
            # Сохраняем счётчик попыток, даже если доставка не удалась.
            try:
                await session.commit()
            except Exception:
                logger.exception(
                    f"Could not persist webhook attempt for payment {payment.id}"
                )
            logger.warning(
                f"Webhook attempt {payment.webhook_attempts} failed for "
                f"payment {payment.id}: {exc}"
            )
            raise WebhookDeliveryError(str(exc)) from exc

        payment.webhook_delivered = True
        await session.commit()
        logger.info(
            f"Payment {payment.id} notification completed after "
            f"{payment.webhook_attempts} attempt(s)"
        )


async def _schedule_retry(
    event: PaymentCreatedEvent,
    next_attempt: int,
    queue: RabbitQueue,
    original_headers: Mapping[str, Any] | None,
) -> None:
    # Очередь объявляется после старта worker'а, но повторная декларация
    # идемпотентна и защищает от ручного удаления топологии.
    await broker.declare_queue(queue)
    headers: HeadersType = {
        "x-retry-count": next_attempt,
        "x-event-id": str((original_headers or {}).get("x-event-id", "")),
    }
    await broker.publish(
        event.model_dump(mode="json"),
        queue=queue,
        headers=headers,
        message_id=str(event.payment_id),
        persist=True,
    )
    logger.warning(
        f"Payment event {event.payment_id} scheduled for attempt "
        f"{next_attempt} via {queue.name}"
    )


async def _mark_failed_if_pending(payment_id: UUID, reason: str) -> None:
    """Не оставлять платёж в pending, если сообщение уходит в DLQ."""
    try:
        async with async_session() as session:
            await session.execute(
                update(Payment)
                .where(
                    Payment.id == payment_id,
                    Payment.status == PaymentStatusEnum.PENDING,
                )
                .values(
                    status=PaymentStatusEnum.FAILED,
                    processed_at=datetime.now(UTC),
                    failure_reason=reason[:2000],
                )
            )
            await session.commit()
    except Exception:
        logger.exception(f"Could not mark payment {payment_id} as failed")


@broker.subscriber(
    queue=PAYMENTS_QUEUE,
    exchange=PAYMENTS_EXCHANGE,
    channel=PAYMENTS_CHANNEL,
    ack_policy=AckPolicy.REJECT_ON_ERROR,
)
async def process_payment_created(
    event: PaymentCreatedEvent,
    message: RabbitMessage,
) -> None:
    """Единственный обработчик платежей с broker-level retry и DLQ."""
    attempt = read_retry_count(message.headers)

    try:
        await _process_payment(event.payment_id)
    except PaymentNotFoundError:
        logger.exception(
            f"Permanent error for event {event.payment_id}: "
            "payment not found; sending to DLQ"
        )
        raise
    except Exception as exc:
        if attempt >= settings.MAX_PROCESSING_ATTEMPTS:
            logger.exception(
                f"Payment {event.payment_id} failed after {attempt} attempts; "
                "sending to DLQ"
            )
            await _mark_failed_if_pending(
                event.payment_id,
                f"{type(exc).__name__}: {exc}",
            )
            raise

        next_attempt = attempt + 1
        queue = retry_queue_for_attempt(next_attempt)
        try:
            await _schedule_retry(event, next_attempt, queue, message.headers)
        except Exception:
            logger.exception(
                f"Could not schedule retry for payment {event.payment_id}; "
                "sending to DLQ"
            )
            await _mark_failed_if_pending(
                event.payment_id,
                f"Could not schedule retry: {exc}",
            )
            raise
        logger.warning(
            f"Payment {event.payment_id} attempt {attempt} failed ({exc}); "
            f"retry {next_attempt} in {queue.name}"
        )
