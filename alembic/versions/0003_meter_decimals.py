"""providers.meter_decimals — per-provider reading precision (water=3, gas=2)

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-14
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default="0" so existing rows are backfilled; cli seed sets water=3/gas=2.
    op.add_column(
        "providers",
        sa.Column(
            "meter_decimals", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    # Widen reading columns to 3 decimals (water); Numeric(12,2) rounded the 3rd away.
    # batch mode for SQLite (which can't ALTER COLUMN TYPE in place).
    with op.batch_alter_table("meter_readings") as batch:
        batch.alter_column("value", type_=sa.Numeric(precision=14, scale=3))
        batch.alter_column(
            "consumption_delta", type_=sa.Numeric(precision=14, scale=3)
        )


def downgrade() -> None:
    with op.batch_alter_table("meter_readings") as batch:
        batch.alter_column("value", type_=sa.Numeric(precision=12, scale=2))
        batch.alter_column(
            "consumption_delta", type_=sa.Numeric(precision=12, scale=2)
        )
    op.drop_column("providers", "meter_decimals")
