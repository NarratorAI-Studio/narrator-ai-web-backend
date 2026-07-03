"""add code column to pricing_quotes_v2 and pricing_snapshots_v2

Revision ID: 20260604_0001
Revises: 20260602_0001
Create Date: 2026-06-04

The implementation requirement: `code` (e.g. `xy0178`) is the canonical upstream identifier
for movie-baokuan templates. The local `template_id` column is the
numeric suffix (`int(code[2:])`) and joins to `pricing_catalog_v2_entry`;
it is NOT the same as the narrator `CSV.id` that web caller reseller-
list pages display. Storing `code` alongside the derived `template_id`
lets the snapshot binding match by code when both quote and master-
task body carry one, and keeps the audit trail honest about which
upstream item was priced.

The column is additive and nullable: legacy quotes / snapshots stay
valid with `code IS NULL` and the snapshot binding falls back to
`template_id` for those rows.
"""

from __future__ import annotations

from alembic import op


revision = "20260604_0001"
down_revision = "20260602_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS: both columns were retrofitted into
    # `pricing_quote_v2/schema.py` CREATE TABLE, so a fresh DB
    # (Backend API contract test cluster) already has them;
    # without the guard, ADD raises `DuplicateColumn`. No-op for prod.
    op.execute("ALTER TABLE pricing_quotes_v2 ADD COLUMN IF NOT EXISTS code TEXT")
    op.execute("ALTER TABLE pricing_snapshots_v2 ADD COLUMN IF NOT EXISTS code TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE pricing_snapshots_v2 DROP COLUMN code")
    op.execute("ALTER TABLE pricing_quotes_v2 DROP COLUMN code")
