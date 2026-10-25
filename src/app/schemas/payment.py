from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.models.enums import CurrencyEnum, PaymentStatusEnum


# Схема для создания платежа
class PaymentCreate(BaseModel):
    amount: Decimal = Field(..., gt=0, decimal_places=2, description="Сумма платежа")
    currency: CurrencyEnum = Field(..., description="Валюта (RUB, USD, EUR)")
    description: str = Field(..., max_length=255, description="Описание платежа")
    metadata_: dict[str, Any] | None = Field(
        default=None, alias="metadata", description="Дополнительные метаданные (JSON)"
    )
    webhook_url: HttpUrl = Field(..., description="URL для webhook-уведомления")

    model_config = ConfigDict(from_attributes=True)


# Схема для ответа при создании платежа (202 Accepted)
class PaymentResponse(BaseModel):
    id: UUID = Field(..., description="ID платежа")
    status: PaymentStatusEnum = Field(..., description="Статус платежа")
    created_at: datetime = Field(..., description="Дата создания")

    model_config = ConfigDict(from_attributes=True)


# Схема для детального ответа о платеже (GET)
class PaymentDetailResponse(BaseModel):
    id: UUID
    amount: Decimal
    currency: CurrencyEnum
    description: str
    # Читаем из атрибута ORM 'metadata_', а в JSON отдаем как 'metadata'
    metadata: dict[str, Any] | None = Field(
        alias="metadata_", serialization_alias="metadata"
    )
    status: PaymentStatusEnum
    idempotency_key: str
    webhook_url: str
    created_at: datetime
    processed_at: datetime | None

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
