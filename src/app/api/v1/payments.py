import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Response, status

from app.api.deps import ApiKey, DbSession
from app.schemas.payment import PaymentCreate, PaymentDetailResponse, PaymentResponse
from app.services.errors import IdempotencyConflictError, PaymentNotFoundError
from app.services.payments import create_payment, get_payment

router = APIRouter(prefix="/payments", tags=["payments"])
logger = logging.getLogger(__name__)


@router.post(
    "",
    response_model=PaymentResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Создание платежа",
    responses={
        401: {"description": "Неверный или отсутствующий X-API-Key"},
        409: {"description": "Idempotency-Key уже использован с другим телом"},
    },
)
async def create_payment_endpoint(
    payment_data: PaymentCreate,
    response: Response,
    db: DbSession,
    api_key: ApiKey,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=255,
            description="Уникальный ключ запроса для защиты от дублей",
        ),
    ],
) -> PaymentResponse:
    """Создаёт платёж и атомарно ставит событие в outbox.

    Повторный запрос с тем же ключом и тем же телом возвращает существующий
    платёж и заголовок ``Idempotency-Replayed: true``.
    """
    key = idempotency_key.strip()
    if not key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Idempotency-Key must not be blank",
        )

    try:
        payment, replayed = await create_payment(db, payment_data, key)
    except IdempotencyConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key was already used with a different payload",
        ) from None

    response.headers["Location"] = f"/api/v1/payments/{payment.id}"
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    logger.info(f"Payment accepted: {payment.id} (replayed={replayed})")
    return PaymentResponse.model_validate(payment)


@router.get(
    "/{payment_id}",
    response_model=PaymentDetailResponse,
    summary="Получение информации о платеже",
    responses={
        401: {"description": "Неверный или отсутствующий X-API-Key"},
        404: {"description": "Платёж не найден"},
    },
)
async def get_payment_endpoint(
    payment_id: UUID,
    db: DbSession,
    api_key: ApiKey,
) -> PaymentDetailResponse:
    try:
        payment = await get_payment(db, payment_id)
    except PaymentNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment not found",
        ) from None
    return PaymentDetailResponse.model_validate(payment)
