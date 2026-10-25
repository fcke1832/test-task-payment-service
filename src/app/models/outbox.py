import uuid
from datetime import datetime

from sqlalchemy import Enum as SAEnum
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base
from app.models.enums import OutboxStatusEnum


class Outbox(Base):
    __tablename__ = "outbox"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Тип события (например, "payment.created")
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)

    # Тело события (JSON)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Статус обработки (pending -> published)
    status: Mapped[OutboxStatusEnum] = mapped_column(
        SAEnum(OutboxStatusEnum, name="outbox_status_type", create_constraint=True),
        default=OutboxStatusEnum.PENDING,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        default=func.now(), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Outbox(id={self.id}, event_type={self.event_type}, status={self.status})>"
