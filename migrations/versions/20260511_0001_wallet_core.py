"""create wallet core tables

Revision ID: 20260511_0001
Revises:
Create Date: 2026-05-11
"""

from __future__ import annotations

from alembic import op

from wallet.schema import WALLET_SCHEMA_SQL


revision = "20260511_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(WALLET_SCHEMA_SQL)


def downgrade() -> None:
    # Wallet tables contain audit evidence; rollback is forward-fix only.
    return None
