"""add processing/webhook fields and outbox retry metadata

Revision ID: b7c1f4a29e10
Revises: 7491e9b8a62e
Create Date: 2026-10-26 18:40:00.000000

Миграция совместима с уже существующей первой версией схемы:
переименовывает значения enum в нижний регистр, расширяет сумму, добавляет
поля статуса webhook и retry-метаданные outbox.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7c1f4a29e10"
down_revision: str | Sequence[str] | None = "7491e9b8a62e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rename_enum_value(enum_name: str, old: str, new: str) -> None:
    """Переименовать label enum'а, если он ещё не переименован."""
    labels = list(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT e.enumlabel "
                "FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = :enum_name"
            ),
            {"enum_name": enum_name},
        )
        .scalars()
    )
    if old in labels:
        op.execute(f"ALTER TYPE \"{enum_name}\" RENAME VALUE '{old}' TO '{new}'")


def upgrade() -> None:
    _rename_enum_value("payment_status_type", "PENDING", "pending")
    _rename_enum_value("payment_status_type", "SUCCEEDED", "succeeded")
    _rename_enum_value("payment_status_type", "FAILED", "failed")
    _rename_enum_value("outbox_status_type", "PENDING", "pending")
    _rename_enum_value("outbox_status_type", "PUBLISHED", "published")
    _rename_enum_value("outbox_status_type", "FAILED", "failed")

    op.alter_column(
        "payments",
        "amount",
        existing_type=sa.Numeric(precision=12, scale=2),
        type_=sa.Numeric(precision=18, scale=2),
        existing_nullable=False,
    )
    op.execute("UPDATE payments SET metadata = '{}'::jsonb WHERE metadata IS NULL")
    op.alter_column(
        "payments",
        "metadata",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    )
    op.alter_column(
        "payments",
        "status",
        server_default=sa.text("'pending'"),
        existing_nullable=False,
    )
    op.add_column(
        "payments",
        sa.Column(
            "webhook_delivered",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "payments",
        sa.Column(
            "webhook_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column("payments", sa.Column("failure_reason", sa.Text(), nullable=True))

    op.alter_column(
        "payments",
        "created_at",
        existing_type=sa.DateTime(),
        existing_server_default=sa.text("now()"),
        server_default=None,
    )
    op.alter_column(
        "payments",
        "created_at",
        existing_type=sa.DateTime(),
        type_=sa.DateTime(timezone=True),
        postgresql_using="created_at AT TIME ZONE 'UTC'",
    )
    op.alter_column(
        "payments",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
    )
    op.alter_column(
        "payments",
        "processed_at",
        existing_type=sa.DateTime(),
        type_=sa.DateTime(timezone=True),
        postgresql_using="processed_at AT TIME ZONE 'UTC'",
    )
    op.create_check_constraint(
        "ck_payments_amount_positive",
        "payments",
        "amount > 0",
    )
    op.create_index(op.f("ix_payments_status"), "payments", ["status"], unique=False)

    op.add_column(
        "outbox",
        sa.Column(
            "attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column("outbox", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column(
        "outbox",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column(
        "outbox",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column(
        "outbox",
        "created_at",
        existing_type=sa.DateTime(),
        existing_server_default=sa.text("now()"),
        server_default=None,
    )
    op.alter_column(
        "outbox",
        "created_at",
        existing_type=sa.DateTime(),
        type_=sa.DateTime(timezone=True),
        postgresql_using="created_at AT TIME ZONE 'UTC'",
    )
    op.alter_column(
        "outbox",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
    )
    op.alter_column(
        "outbox",
        "status",
        server_default=sa.text("'pending'"),
        existing_nullable=False,
    )
    op.create_index(
        op.f("ix_outbox_next_attempt_at"),
        "outbox",
        ["next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_outbox_next_attempt_at"), table_name="outbox")
    op.alter_column("outbox", "status", server_default=None)
    op.drop_column("outbox", "published_at")
    op.drop_column("outbox", "next_attempt_at")
    op.drop_column("outbox", "last_error")
    op.drop_column("outbox", "attempts")
    op.alter_column(
        "outbox",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.text("now()"),
        server_default=None,
    )
    op.alter_column(
        "outbox",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        type_=sa.DateTime(),
        postgresql_using="created_at AT TIME ZONE 'UTC'",
    )
    op.alter_column(
        "outbox",
        "created_at",
        existing_type=sa.DateTime(),
        server_default=sa.text("now()"),
    )

    op.drop_index(op.f("ix_payments_status"), table_name="payments")
    op.drop_constraint(
        "ck_payments_amount_positive",
        "payments",
        type_="check",
    )
    op.alter_column(
        "payments",
        "processed_at",
        existing_type=sa.DateTime(timezone=True),
        type_=sa.DateTime(),
        postgresql_using="processed_at AT TIME ZONE 'UTC'",
    )
    op.alter_column(
        "payments",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.text("now()"),
        server_default=None,
    )
    op.alter_column(
        "payments",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        type_=sa.DateTime(),
        postgresql_using="created_at AT TIME ZONE 'UTC'",
    )
    op.alter_column(
        "payments",
        "created_at",
        existing_type=sa.DateTime(),
        server_default=sa.text("now()"),
    )
    op.drop_column("payments", "failure_reason")
    op.drop_column("payments", "webhook_attempts")
    op.drop_column("payments", "webhook_delivered")
    op.alter_column("payments", "status", server_default=None)
    op.alter_column(
        "payments",
        "metadata",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        nullable=True,
    )
    op.alter_column(
        "payments",
        "amount",
        existing_type=sa.Numeric(precision=18, scale=2),
        type_=sa.Numeric(precision=12, scale=2),
        existing_nullable=False,
    )

    _rename_enum_value("payment_status_type", "pending", "PENDING")
    _rename_enum_value("payment_status_type", "succeeded", "SUCCEEDED")
    _rename_enum_value("payment_status_type", "failed", "FAILED")
    _rename_enum_value("outbox_status_type", "pending", "PENDING")
    _rename_enum_value("outbox_status_type", "published", "PUBLISHED")
    _rename_enum_value("outbox_status_type", "failed", "FAILED")
