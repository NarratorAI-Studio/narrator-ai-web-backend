"""add source_sheet_id column to fa_template_price for code-based join

Revision ID: 20260525_0001
Revises: 20260518_0001
Create Date: 2026-05-25

Adds an indexed `source_sheet_id` column (e.g. 'sample-0001') so the
upstream endpoint can JOIN upstream `/v2/res/movie-baokuan` items to local
template price rows via the external catalog code instead of template_id (the
two PKs are independent numbering schemes — upstream item.id is from
res_movie_baokuan, fa_template_price.template_id is the seed PK).

Re-run `scripts/backfill_template_price.py` after `alembic upgrade head`
to populate source_sheet_id from `scripts/seeds/template_price_seed.json`.
"""

from __future__ import annotations

from alembic import op


revision = "20260525_0001"
down_revision = "20260518_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE fa_template_price ADD COLUMN IF NOT EXISTS source_sheet_id TEXT"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS fa_template_price_source_sheet_id_idx "
        "ON fa_template_price (source_sheet_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS fa_template_price_source_sheet_id_idx")
    op.execute("ALTER TABLE fa_template_price DROP COLUMN IF EXISTS source_sheet_id")
