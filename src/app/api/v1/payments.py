from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, status
from sqlalchemy import select

from app.api.deps import ApiKey, DbSession
from app.models.enums import OutboxStatusEnum, PaymentStatusEnum
from app.models.outbox import Outbox
from app.models.payment import Payment
from app.schemas.payment import PaymentCreate, PaymentDetailResponse, PaymentResponse

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post(
    "",
    response_model=PaymentResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Создание платежа",
)
async def create_payment(
    payment_data: PaymentCreate,
    idempotency_key: Annotated[str, Header()],
    db: DbSession,
    api_key: ApiKey,
) -> PaymentResponse:
    """
    Создает новый платеж или возвращает существующий при повторном запросе
    с тем же idempotency_key.
    """
    # Проверяем идемпотентность: ищем платеж с таким ключом
    stmt = select(Payment).where(Payment.idempotency_key == idempotency_key)
    result: any = await db.execute(stmt)
    existing_payment: Payment | None = result.scalar_one_or_none()

    # Если платеж уже существует, возвращаем его (идемпотентность)
    if existing_payment:
        return existing_payment

    # Создаем новый платеж
    new_payment: Payment = Payment(
        amount=payment_data.amount,
        currency=payment_data.currency,
        description=payment_data.description,
        metadata_=payment_data.metadata_ or {},
        webhook_url=str(payment_data.webhook_url),
        idempotency_key=idempotency_key,
        status=PaymentStatusEnum.PENDING,
    )
    db.add(new_payment)

    # Создаем запись в Outbox для публикации события
    outbox_event: Outbox = Outbox(
        event_type="payment.created",
        payload={
            "payment_id": str(new_payment.id),
            "amount": str(new_payment.amount),
            "currency": new_payment.currency.value,
        },
        status=OutboxStatusEnum.PENDING,
    )
    db.add(outbox_event)

    # Коммитим обе записи в одной транзакции (Transactional Outbox pattern)
    await db.flush()  # Получаем ID платежа до коммита

    return new_payment


@router.get(
    "/{payment_id}",
    response_model=PaymentDetailResponse,
    summary="Получение информации о платеже",
)
async def get_payment(
    payment_id: UUID,
    db: DbSession,
    api_key: ApiKey,
) -> PaymentDetailResponse:
    """
    Возвращает детальную информацию о платеже по его ID.
    """
    stmt = select(Payment).where(Payment.id == payment_id)
    result: any = await db.execute(stmt)
    payment: Payment | None = result.scalar_one_or_none()

    if not payment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found"
        )

    return payment
