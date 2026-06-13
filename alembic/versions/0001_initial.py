"""initial schema: providers, provider_patterns, payments, nudge_logs

Revision ID: 0001
Revises:
Create Date: 2026-06-14
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "providers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "category",
            sa.Enum(
                "water", "electricity", "gas", "internet", "housing", "mobile",
                name="category",
            ),
            nullable=False,
        ),
        sa.Column("account_number", sa.String(length=64), nullable=True),
        sa.Column(
            "pay_channel",
            sa.Enum("mono_communal", "mono_card", "off_mono", name="pay_channel"),
            nullable=False,
        ),
        sa.Column("expected_amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("due_day", sa.Integer(), nullable=True),
        sa.Column("auto_logged", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "provider_patterns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("pattern", sa.String(length=255), nullable=False),
        sa.Column(
            "source",
            sa.Enum("seed", "learned", name="pattern_source"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_id", "pattern", name="uq_provider_pattern"),
    )
    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=True),
        sa.Column("amount_uah", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "source",
            sa.Enum("mono_webhook", "manual", name="payment_source"),
            nullable=False,
        ),
        sa.Column("raw_description", sa.String(length=512), nullable=False),
        sa.Column("mcc", sa.Integer(), nullable=True),
        sa.Column("mono_tx_id", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mono_tx_id"),
    )
    op.create_table(
        "nudge_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("cycle", sa.String(length=7), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("payment", "meter", name="nudge_kind"),
            nullable=False,
        ),
        sa.Column("nudged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("nudge_logs")
    op.drop_table("payments")
    op.drop_table("provider_patterns")
    op.drop_table("providers")
