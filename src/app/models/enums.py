import enum


class PaymentStatusEnum(enum.StrEnum):
    """Статус обработки платежа."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CurrencyEnum(enum.StrEnum):
    """Поддерживаемые валюты."""

    RUB = "RUB"
    USD = "USD"
    EUR = "EUR"


class OutboxStatusEnum(enum.StrEnum):
    """Статус публикации outbox-события."""

    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"
