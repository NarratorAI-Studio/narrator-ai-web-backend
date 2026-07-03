"""add pricing_quotes_v2 + pricing_snapshots_v2 + narrator_tasks.snapshot_id

Revision ID: 20260601_0001
Revises: 20260529_0001
Create Date: 2026-06-01

Step 3 of the template price v2 backend support stream (the implementation requirement).
Three changes in one migration:

1. `pricing_quotes_v2` — every reported quote.
2. `pricing_snapshots_v2` — immutable per-order record on commit.
3. `narrator_tasks.snapshot_id` — nullable FK so v2 master-task rows
   can join to their snapshot. v1 rows keep `snapshot_id IS NULL` and
   behave exactly as before.

Tables stay additive — v1's `fa_template_price` and `wallet_quotes`
are not touched. The contract for the new shape lives in
the public quote snapshot contract.

NOTE: the DDL below is intentionally inlined as a **frozen snapshot**
of `pricing_quote_v2.schema` as of 2026-06-01. Originally this module
imported `ALL_SCHEMA_SQL` from the live `pricing_quote_v2.schema`,
but that coupled this migration to whatever future schema additions
the package gained — running `alembic upgrade head` on a fresh DB
would re-create the table with the latest columns, then a later
migration's `ALTER TABLE ADD COLUMN` for the same column would fail
with a duplicate-column error. Frozen DDL here means migrations are
time-coupled to when they shipped, not to what the schema module looks
like today.
"""

from __future__ import annotations

from alembic import op


revision = "20260601_0001"
down_revision = "20260529_0001"
branch_labels = None
depends_on = None


# Frozen at 2026-06-01 — does NOT pick up later schema columns such as
# `custom_srt_file_id` (added in 20260602_0001).
_PRICING_QUOTES_V2_SCHEMA_SQL_FROZEN = """
CREATE TABLE IF NOT EXISTS pricing_quotes_v2 (
    quote_id TEXT PRIMARY KEY,
    pricing_rule_version TEXT NOT NULL,
    price_source TEXT NOT NULL CHECK (price_source IN ('manual_catalog_price', 'system_calculated_price')),
    template_id TEXT,
    custom_template_id TEXT,
    combo_key TEXT NOT NULL,
    pro_upgrade BOOLEAN NOT NULL DEFAULT FALSE,
    starting_price INTEGER,
    final_charge_price INTEGER NOT NULL CHECK (final_charge_price >= 0),
    flash_total INTEGER NOT NULL CHECK (flash_total >= 0),
    pro_total INTEGER NOT NULL CHECK (pro_total >= 0),
    pro_upgrade_delta INTEGER NOT NULL CHECK (pro_upgrade_delta >= 0),
    pricing_minutes NUMERIC(10, 4) NOT NULL,
    valid_line_count INTEGER,
    srt_file_hash TEXT,
    system_reference_price INTEGER NOT NULL CHECK (system_reference_price >= 0),
    breakdown TEXT NOT NULL,
    currency_unit TEXT NOT NULL DEFAULT 'web_point',
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    committed_at TIMESTAMP WITH TIME ZONE,
    web_user_id INTEGER NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT exactly_one_template_id
        CHECK ((template_id IS NOT NULL) <> (custom_template_id IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS pricing_quotes_v2_user_idx
    ON pricing_quotes_v2 (web_user_id, created_at DESC);
"""


_PRICING_SNAPSHOTS_V2_SCHEMA_SQL_FROZEN = """
CREATE TABLE IF NOT EXISTS pricing_snapshots_v2 (
    snapshot_id TEXT PRIMARY KEY,
    quote_id TEXT NOT NULL UNIQUE,
    pricing_rule_version TEXT NOT NULL,
    combo_key TEXT NOT NULL,
    price_source TEXT NOT NULL,
    template_id TEXT,
    custom_template_id TEXT,
    template_duration NUMERIC(10, 4),
    pricing_minutes NUMERIC(10, 4) NOT NULL,
    valid_line_count INTEGER,
    srt_file_hash TEXT,
    system_reference_price INTEGER NOT NULL,
    manual_catalog_price INTEGER,
    system_calculated_price INTEGER,
    final_charge_price INTEGER NOT NULL CHECK (final_charge_price >= 0),
    breakdown TEXT NOT NULL,
    currency_unit TEXT NOT NULL DEFAULT 'web_point',
    committed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    refund_policy TEXT NOT NULL DEFAULT 'manual' CHECK (refund_policy IN ('manual', 'all', 'partial_by_subflow')),
    refund_status TEXT NOT NULL DEFAULT 'none',
    subflow_status TEXT NOT NULL DEFAULT '[]',
    web_user_id INTEGER NOT NULL,
    FOREIGN KEY (quote_id) REFERENCES pricing_quotes_v2 (quote_id)
);

CREATE INDEX IF NOT EXISTS pricing_snapshots_v2_user_idx
    ON pricing_snapshots_v2 (web_user_id, committed_at DESC);
"""


_NARRATOR_TASKS_SNAPSHOT_LINK_SQL_FROZEN = """
ALTER TABLE narrator_tasks
    ADD COLUMN IF NOT EXISTS snapshot_id TEXT REFERENCES pricing_snapshots_v2 (snapshot_id);

CREATE INDEX IF NOT EXISTS narrator_tasks_snapshot_idx
    ON narrator_tasks (snapshot_id) WHERE snapshot_id IS NOT NULL;
"""


_UPGRADE_SQL_FROZEN: tuple[str, ...] = (
    _PRICING_QUOTES_V2_SCHEMA_SQL_FROZEN,
    _PRICING_SNAPSHOTS_V2_SCHEMA_SQL_FROZEN,
    _NARRATOR_TASKS_SNAPSHOT_LINK_SQL_FROZEN,
)


_DOWNGRADE_SQL_FROZEN: tuple[str, ...] = (
    "DROP INDEX IF EXISTS narrator_tasks_snapshot_idx;",
    "ALTER TABLE narrator_tasks DROP COLUMN IF EXISTS snapshot_id;",
    "DROP INDEX IF EXISTS pricing_snapshots_v2_user_idx;",
    "DROP TABLE IF EXISTS pricing_snapshots_v2;",
    "DROP INDEX IF EXISTS pricing_quotes_v2_user_idx;",
    "DROP TABLE IF EXISTS pricing_quotes_v2;",
)


def upgrade() -> None:
    for sql in _UPGRADE_SQL_FROZEN:
        op.execute(sql)


def downgrade() -> None:
    for sql in _DOWNGRADE_SQL_FROZEN:
        op.execute(sql)
