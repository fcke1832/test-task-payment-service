from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel

from app.models.enums import CurrencyEnum


class PaymentCreatedEvent(BaseModel):
    payment_id: UUID
    amount: Decimal
    currency: CurrencyEnum
