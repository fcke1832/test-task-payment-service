import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base
from app.models.enums import CurrencyEnum, PaymentStatusEnum


class Payment(Base):
    __tablename__ = "payments"

    # Уникальный идентификатор (UUID)
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Сумма (используем Numeric для точности, избегаем Float!)
    amount: Mapped[Decimal] = mapped_column(Numeric(precision=12, scale=2))

    # Валюта (нативный ENUM в Postgres)
    currency: Mapped[CurrencyEnum] = mapped_column(
        SAEnum(CurrencyEnum, name="currency_type", create_constraint=True)
    )

    description: Mapped[str] = mapped_column(String(255))

    # Метаданные (JSONB).
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", JSONB, default=dict, server_default="{}"
    )

    # Статус платежа
    status: Mapped[PaymentStatusEnum] = mapped_column(
        SAEnum(PaymentStatusEnum, name="payment_status_type", create_constraint=True),
        default=PaymentStatusEnum.PENDING,
    )

    # Ключ идемпотентности (защита от дублей)
    idempotency_key: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )

    webhook_url: Mapped[str] = mapped_column(String(2048), nullable=False)

    # Даты
    created_at: Mapped[datetime] = mapped_column(
        default=func.now(), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    def __repr__(self) -> str:
        return f"<Payment(id={self.id}, status={self.status}, amount={self.amount})>"
