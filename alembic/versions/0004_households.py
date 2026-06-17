"""households: per-property providers, static meter value

Adds a `households` table and makes providers belong to a household. The `name` unique
becomes composite (household_id, name) so shared utilities (ЛЕЗ, Газ доставлення) can
exist once per property. Existing rows are backfilled to a primary household; the seed
later fills its real name/infolviv account from env.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-17
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Rename the reflected unnamed UNIQUE(name) deterministically so batch mode can drop it
# on SQLite (which stores it as an auto-index with a generated name).
_NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def upgrade() -> None:
    op.create_table(
        "households",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "is_primary", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("infolviv_account_code", sa.String(length=64), nullable=True),
        sa.UniqueConstraint("slug", name="uq_households_slug"),
    )
    # A primary household so existing providers have an owner; seed fills the real name.
    op.execute(
        "INSERT INTO households (slug, name, is_primary) VALUES ('primary', 'Житло 1', 1)"
    )

    with op.batch_alter_table("providers", naming_convention=_NAMING) as batch:
        batch.add_column(sa.Column("household_id", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("static_reading", sa.Numeric(precision=14, scale=3), nullable=True)
        )
        batch.create_foreign_key(
            "fk_providers_household_id", "households", ["household_id"], ["id"]
        )
        batch.drop_constraint("uq_providers_name", type_="unique")
        batch.create_unique_constraint(
            "uq_provider_household_name", ["household_id", "name"]
        )

    op.execute(
        "UPDATE providers SET household_id = "
        "(SELECT id FROM households WHERE slug = 'primary')"
    )


def downgrade() -> None:
    with op.batch_alter_table("providers", naming_convention=_NAMING) as batch:
        batch.drop_constraint("uq_provider_household_name", type_="unique")
        batch.drop_constraint("fk_providers_household_id", type_="foreignkey")
        batch.create_unique_constraint("uq_providers_name", ["name"])
        batch.drop_column("static_reading")
        batch.drop_column("household_id")
    op.drop_table("households")
