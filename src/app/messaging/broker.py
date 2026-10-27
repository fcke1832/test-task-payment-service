"""Топология RabbitMQ для асинхронной обработки платежей.

Основной поток:
``payments.exchange`` --(payment.created)--> ``payments.new`` --> consumer.

Ошибки:
- consumer публикует сообщение в ``payments.retry.<attempt>`` (default exchange);
- retry-очередь держит сообщение TTL = 1s, 2s, ... и возвращает его в
  ``payments.exchange``;
- после исчерпания попыток сообщение отклоняется (requeue=False) и через
  ``payments.dead.exchange`` попадает в ``payments.new.dlq``.
"""

from faststream.rabbit import (
    Channel,
    ExchangeType,
    RabbitBroker,
    RabbitExchange,
    RabbitQueue,
)

from app.core.config import settings

PAYMENTS_EXCHANGE_NAME = "payments.exchange"
PAYMENTS_QUEUE_NAME = "payments.new"
PAYMENTS_ROUTING_KEY = "payment.created"

DEAD_EXCHANGE_NAME = "payments.dead.exchange"
DEAD_QUEUE_NAME = "payments.new.dlq"
DEAD_ROUTING_KEY = "payment.dead"

PAYMENTS_EXCHANGE = RabbitExchange(
    PAYMENTS_EXCHANGE_NAME,
    type=ExchangeType.DIRECT,
    durable=True,
)
DEAD_EXCHANGE = RabbitExchange(
    DEAD_EXCHANGE_NAME,
    type=ExchangeType.DIRECT,
    durable=True,
)

PAYMENTS_QUEUE = RabbitQueue(
    PAYMENTS_QUEUE_NAME,
    durable=True,
    auto_delete=False,
    routing_key=PAYMENTS_ROUTING_KEY,
    arguments={
        "x-dead-letter-exchange": DEAD_EXCHANGE_NAME,
        "x-dead-letter-routing-key": DEAD_ROUTING_KEY,
    },
)
DEAD_QUEUE = RabbitQueue(
    DEAD_QUEUE_NAME,
    durable=True,
    auto_delete=False,
    routing_key=DEAD_ROUTING_KEY,
)

# Один consumer и prefetch=1: сообщения обрабатываются последовательно.
PAYMENTS_CHANNEL = Channel(prefetch_count=1)


def build_retry_queues() -> tuple[RabbitQueue, ...]:
    """Очереди экспоненциальной задержки для 2-й, 3-й, ... попыток.

    TTL первой retry-очереди равен ``RETRY_BASE_DELAY_SECONDS``, каждой
    следующей — в два раза больше. Очередь не имеет consumer'а: RabbitMQ
    сам возвращает сообщение в основной exchange после истечения TTL.
    """
    queues: list[RabbitQueue] = []
    for attempt in range(2, settings.MAX_PROCESSING_ATTEMPTS + 1):
        delay_seconds = settings.RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 2))
        queues.append(
            RabbitQueue(
                f"payments.retry.{attempt}",
                durable=True,
                auto_delete=False,
                arguments={
                    "x-message-ttl": max(1, int(delay_seconds * 1000)),
                    "x-dead-letter-exchange": PAYMENTS_EXCHANGE_NAME,
                    "x-dead-letter-routing-key": PAYMENTS_ROUTING_KEY,
                },
            )
        )
    return tuple(queues)


RETRY_QUEUES = build_retry_queues()

broker = RabbitBroker(
    settings.RABBITMQ_URL,
    # Publisher confirm + ошибка, если mandatory-сообщение не попало ни в одну
    # очередь. Outbox не должен помечать событие published в таком случае.
    default_channel=Channel(publisher_confirms=True, on_return_raises=True),
)
