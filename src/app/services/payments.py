import logging
from decimal import Decimal
from uuid import UUID

from pydantic import HttpUrl
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import OutboxStatusEnum, PaymentStatusEnum
from app.models.outbox import Outbox
from app.models.payment import Payment
from app.schemas.payment import PaymentCreate
from app.services.errors import IdempotencyConflictError, PaymentNotFoundError

logger = logging.getLogger(__name__)


def _webhook_url(value: HttpUrl) -> str:
    return str(value)


def _ensure_same_request(payment: Payment, data: PaymentCreate) -> None:
    """Один Idempotency-Key должен однозначно идентифицировать один запрос."""
    same_request = (
        Decimal(payment.amount) == data.amount
        and payment.currency == data.currency
        and payment.description == data.description
        and (payment.metadata_ or {}) == data.metadata
        and payment.webhook_url == _webhook_url(data.webhook_url)
    )
    if not same_request:
        raise IdempotencyConflictError


async def get_payment(session: AsyncSession, payment_id: UUID) -> Payment:
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise PaymentNotFoundError
    return payment


async def create_payment(
    session: AsyncSession,
    data: PaymentCreate,
    idempotency_key: str,
) -> tuple[Payment, bool]:
    """Создать платёж и outbox-событие в одной транзакции.

    Возвращает ``(payment, replayed)``. При повторном запросе с тем же ключом
    и тем же телом возвращает уже созданный платёж и ``replayed=True``.
    """
    stmt = select(Payment).where(Payment.idempotency_key == idempotency_key)
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        _ensure_same_request(existing, data)
        return existing, True

    payment = Payment(
        amount=data.amount,
        currency=data.currency,
        description=data.description,
        metadata_=data.metadata,
        webhook_url=_webhook_url(data.webhook_url),
        idempotency_key=idempotency_key,
        status=PaymentStatusEnum.PENDING,
    )
    session.add(payment)

    try:
        # flush нужен, чтобы получить UUID до создания outbox-события.
        await session.flush()
        session.add(
            Outbox(
                event_type="payment.created",
                payload={
                    "payment_id": str(payment.id),
                    "amount": format(payment.amount, ".2f"),
                    "currency": payment.currency.value,
                },
                status=OutboxStatusEnum.PENDING,
            )
        )
        await session.commit()
    except IntegrityError:
        # Возможна гонка двух одновременных запросов с одинаковым ключом:
        # уникальный индекс БД отдаёт победу только одному из них.
        await session.rollback()
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            raise
        _ensure_same_request(existing, data)
        return existing, True
    except Exception:
        await session.rollback()
        raise

    await session.refresh(payment)
    logger.info(f"Payment created: {payment.id}")
    return payment, False
