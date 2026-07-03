"""create fa_template_price table for template price quote/pin adapter

Revision ID: 20260518_0001
Revises: 20260511_0001
Create Date: 2026-05-18
"""

from __future__ import annotations

from alembic import op

from pricing.schema import TEMPLATE_PRICE_SCHEMA_SQL


revision = "20260518_0001"
down_revision = "20260511_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(TEMPLATE_PRICE_SCHEMA_SQL)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS fa_template_price_current_unique")
    op.execute("DROP INDEX IF EXISTS fa_template_price_current_idx")
    op.execute("DROP TABLE IF EXISTS fa_template_price")
