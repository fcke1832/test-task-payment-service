from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.models.enums import CurrencyEnum, PaymentStatusEnum


class PaymentCreate(BaseModel):
    """Тело запроса на создание платежа."""

    amount: Decimal = Field(
        ...,
        gt=0,
        max_digits=18,
        decimal_places=2,
        description="Сумма платежа (не более 2 знаков после запятой)",
    )
    currency: CurrencyEnum = Field(..., description="Валюта: RUB, USD или EUR")
    description: str = Field(..., min_length=1, max_length=255)
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Произвольные метаданные платежа",
    )
    webhook_url: HttpUrl = Field(
        ..., max_length=2048, description="URL для уведомления о результате"
    )

    model_config = ConfigDict(extra="forbid")


class PaymentResponse(BaseModel):
    """Ответ на создание платежа (202 Accepted)."""

    payment_id: UUID = Field(..., validation_alias="id")
    status: PaymentStatusEnum
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class PaymentDetailResponse(BaseModel):
    """Детальная информация о платеже."""

    payment_id: UUID = Field(..., validation_alias="id")
    amount: Decimal
    currency: CurrencyEnum
    description: str
    metadata: dict[str, Any] = Field(..., validation_alias="metadata_")
    status: PaymentStatusEnum
    idempotency_key: str
    webhook_url: str
    webhook_delivered: bool
    webhook_attempts: int
    failure_reason: str | None
    created_at: datetime
    processed_at: datetime | None

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
