import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func, text

from app.models.base import Base
from app.models.enums import CurrencyEnum, PaymentStatusEnum


def enum_values(enum_class: type[enum.Enum]) -> list[str]:
    """Хранить в PostgreSQL значения enum, а не имена Python-констант."""
    return [member.value for member in enum_class]


class Payment(Base):
    """Платёж и состояние его обработки."""

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=2))
    currency: Mapped[CurrencyEnum] = mapped_column(
        SAEnum(
            CurrencyEnum,
            name="currency_type",
            values_callable=enum_values,
            create_constraint=True,
        )
    )
    description: Mapped[str] = mapped_column(String(255))
    metadata_: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )
    status: Mapped[PaymentStatusEnum] = mapped_column(
        SAEnum(
            PaymentStatusEnum,
            name="payment_status_type",
            values_callable=enum_values,
            create_constraint=True,
        ),
        default=PaymentStatusEnum.PENDING,
        server_default=PaymentStatusEnum.PENDING.value,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    webhook_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    webhook_delivered: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    webhook_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return (
            f"<Payment(id={self.id}, status={self.status}, "
            f"amount={self.amount} {self.currency})>"
        )
