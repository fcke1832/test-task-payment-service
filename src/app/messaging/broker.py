"""
Модуль описывает топологию RabbitMQ для обработки платежей.

Архитектура:
1. PAYMENTS_EXCHANGE (payments.exchange) - основной exchange для событий создания платежей
2. PAYMENTS_QUEUE (payments.new) - очередь для обработки новых платежей
3. DEAD_EXCHANGE (payments.dead.exchange) - exchange для мертвых писем (DLX)
4. DEAD_QUEUE (payments.new.dlq) - очередь для сообщений, которые не удалось обработать

Dead Letter Mechanism:
Если сообщение не может быть обработано (ошибка, timeout, nack без requeue),
RabbitMQ автоматически перенаправляет его в DLQ через DLX. Это позволяет:
- Не терять сообщения при временных сбоях
- Анализировать проблемные сообщения
- Вручную переотправлять после исправления проблем
"""

from faststream.rabbit import ExchangeType, RabbitBroker, RabbitExchange, RabbitQueue

from app.core.config import settings

# === Exchange и Routing Keys ===

# Название основного exchange для платежей
PAYMENTS_EXCHANGE_NAME: str = "payments.exchange"

# Routing key для событий создания платежа
# Consumer подписывается на этот key для получения событий
PAYMENTS_ROUTING_KEY: str = "payment.created"

# === Dead Letter Exchange (DLX) ===

# Exchange для мертвых писем (Dead Letter Exchange)
# Сюда попадают сообщения, которые не удалось обработать после всех retry
DEAD_EXCHANGE_NAME: str = "payments.dead.exchange"

# Routing key для мертвых писем
DEAD_ROUTING_KEY: str = "payment.dead"

# Создаем DLX exchange
# type=ExchangeType.DIRECT - сообщение попадает в очередь с точным совпадением routing key
# durable=True - exchange переживет перезапуск RabbitMQ
DEAD_EXCHANGE: RabbitExchange = RabbitExchange(
    DEAD_EXCHANGE_NAME,
    type=ExchangeType.DIRECT,
    durable=True,
)

# === Основной Exchange ===

# Exchange для обработки платежей
# Consumer подписывается на PAYMENTS_QUEUE с routing key PAYMENTS_ROUTING_KEY
PAYMENTS_EXCHANGE: RabbitExchange = RabbitExchange(
    PAYMENTS_EXCHANGE_NAME,
    type=ExchangeType.DIRECT,
    durable=True,
)

# === Очереди ===

# Основная очередь для новых платежей
# - durable=True - очередь переживет перезапуск RabbitMQ
# - auto_delete=False - очередь не удалится, когда отключатся все consumers
# - x-dead-letter-exchange - если сообщение не обработано, оно уйдет в DLX
# - x-dead-letter-routing-key - routing key для DLX
PAYMENTS_QUEUE: RabbitQueue = RabbitQueue(
    "payments.new",
    durable=True,
    auto_delete=False,
    arguments={
        "x-dead-letter-exchange": DEAD_EXCHANGE_NAME,
        "x-dead-letter-routing-key": DEAD_ROUTING_KEY,
    },
)

# Очередь мертвых писем (Dead Letter Queue)
# Сюда попадают сообщения, которые не удалось обработать
# Используется для анализа ошибок и ручной обработки
DEAD_QUEUE: RabbitQueue = RabbitQueue(
    "payments.new.dlq",
    durable=True,
    auto_delete=False,
)

# === Broker ===

# FastStream broker - основной объект для работы с RabbitMQ
# Подключается к RabbitMQ по URL из конфигурации
broker: RabbitBroker = RabbitBroker(settings.RABBITMQ_URL)
