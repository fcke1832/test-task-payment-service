from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.enums import CurrencyEnum


class PaymentCreatedEvent(BaseModel):
    """Событие outbox, публикуемое в ``payments.new``."""

    payment_id: UUID
    amount: Decimal
    currency: CurrencyEnum

    model_config = ConfigDict(extra="ignore")
