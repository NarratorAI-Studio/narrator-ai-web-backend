"""create users table for reseller user identity + balance SOT

Revision ID: 20260526_0001
Revises: 20260525_0001
Create Date: 2026-05-26

First step of the reseller user system. `users.balance_points` is the
source-of-truth for end-user balance; `wallet_accounts.available_balance /
frozen_balance` will be refactored to follow this table in a separate issue.

Initial allocation per user is 1000 points (DEFAULT in the schema).
"""

from __future__ import annotations

from alembic import op

from users.schema import USERS_SCHEMA_SQL


revision = "20260526_0001"
down_revision = "20260525_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(USERS_SCHEMA_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS users")
