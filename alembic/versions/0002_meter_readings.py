"""meter readings: providers.meter_window + meter_readings table

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-14
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "providers", sa.Column("meter_window", sa.Integer(), nullable=True)
    )
    op.create_table(
        "meter_readings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=True),
        sa.Column("cycle", sa.String(length=7), nullable=False),
        sa.Column("value", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("ocr_raw", sa.String(length=64), nullable=True),
        sa.Column(
            "consumption_delta", sa.Numeric(precision=12, scale=2), nullable=True
        ),
        sa.Column("photo_ref", sa.String(length=512), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "ocr_pending",
                "needs_confirm",
                "validated",
                "submitted",
                "rejected",
                "failed",
                name="meter_status",
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("meter_readings")
    op.drop_column("providers", "meter_window")
