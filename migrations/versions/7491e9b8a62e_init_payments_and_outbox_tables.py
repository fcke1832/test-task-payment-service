"""initial payments and outbox schema

Revision ID: 7491e9b8a62e
Revises:
Create Date: 2026-10-25 16:28:46.506413

Первая версия схемы. Следующая миграция приводит её к актуальному виду:
добавляет поля обработки/webhook, outbox retry и переименовывает значения
enum в нижний регистр.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7491e9b8a62e"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

currency_type = postgresql.ENUM(
    "RUB",
    "USD",
    "EUR",
    name="currency_type",
    create_type=False,
)
payment_status_type = postgresql.ENUM(
    "PENDING",
    "SUCCEEDED",
    "FAILED",
    name="payment_status_type",
    create_type=False,
)
outbox_status_type = postgresql.ENUM(
    "PENDING",
    "PUBLISHED",
    "FAILED",
    name="outbox_status_type",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    currency_type.create(bind, checkfirst=True)
    payment_status_type.create(bind, checkfirst=True)
    outbox_status_type.create(bind, checkfirst=True)

    op.create_table(
        "outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("status", outbox_status_type, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_outbox_status"), "outbox", ["status"], unique=False)

    op.create_table(
        "payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", currency_type, nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=True,
        ),
        sa.Column("status", payment_status_type, nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("webhook_url", sa.String(length=2048), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_payments_idempotency_key"),
        "payments",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_payments_idempotency_key"), table_name="payments")
    op.drop_table("payments")
    op.drop_index(op.f("ix_outbox_status"), table_name="outbox")
    op.drop_table("outbox")

    bind = op.get_bind()
    outbox_status_type.drop(bind, checkfirst=True)
    payment_status_type.drop(bind, checkfirst=True)
    currency_type.drop(bind, checkfirst=True)
