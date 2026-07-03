"""add pricing_quotes_v2.custom_srt_file_id

Revision ID: 20260602_0001
Revises: 20260601_0002
Create Date: 2026-06-02

This migration binds the quote to the SRT file_id it was priced from, so a caller
can't quote cheap/small file_id-A and then commit a different
expensive file_id-B against the same quote.

The column is nullable because manual-catalog quotes have no SRT.
The `commit_master_task_snapshot` §6.2 quadruple compares the
client-supplied `custom_srt_file_id` against this stored value;
mismatch raises `QUOTE_PARAMETERS_CHANGED` and forces a re-quote.

Backfill is intentionally a no-op because existing v2 quotes had no
`custom_srt_file_id`; leaving NULL is the only honest value. New quote
rows from this revision onward populate the column for custom-template
branches.
"""

from __future__ import annotations

from alembic import op


revision = "20260602_0001"
down_revision = "20260601_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS: the column was retrofitted into
    # `pricing_quote_v2/schema.py` CREATE TABLE, so a fresh DB
    # (Backend API contract test cluster) already has it; without
    # the guard, ADD raises `DuplicateColumn`. No-op for prod.
    op.execute(
        "ALTER TABLE pricing_quotes_v2 ADD COLUMN IF NOT EXISTS custom_srt_file_id TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE pricing_quotes_v2 DROP COLUMN custom_srt_file_id")
