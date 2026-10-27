class IdempotencyConflictError(Exception):
    """Тот же Idempotency-Key использован с другим телом запроса."""


class PaymentNotFoundError(Exception):
    """Платёж не найден."""
