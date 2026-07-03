"""Raw SQL DDL for the v2 quote + snapshot tables .

PostgreSQL-targeted; SQLite-portable for tests (no partial indexes
required on these tables — primary lookups are by exact ID).

Three changes:

1. `pricing_quotes_v2` — every quote.
2. `pricing_snapshots_v2` — immutable per-order record on commit.
3. `narrator_tasks.snapshot_id` — nullable FK to bridge v2 master
   tasks to their snapshot row.
"""

from __future__ import annotations


PRICING_QUOTES_V2_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pricing_quotes_v2 (
    quote_id TEXT PRIMARY KEY,
    pricing_rule_version TEXT NOT NULL,
    price_source TEXT NOT NULL CHECK (price_source IN ('manual_catalog_price', 'system_calculated_price')),
    template_id TEXT,
    -- Canonical upstream xy-code (e.g. "xy0178"). Added by regression coverage so the
    -- snapshot binding can match by code and the audit trail records
    -- what the caller actually sent. Nullable for backwards compat.
    code TEXT,
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
    -- Cloud-drive file_id the SRT was fetched from at quote time.
    -- Bound at commit (§6.2) so a caller can't quote one SRT and
    -- commit a different one.
    custom_srt_file_id TEXT,
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


PRICING_SNAPSHOTS_V2_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pricing_snapshots_v2 (
    snapshot_id TEXT PRIMARY KEY,
    quote_id TEXT NOT NULL UNIQUE,
    pricing_rule_version TEXT NOT NULL,
    combo_key TEXT NOT NULL,
    price_source TEXT NOT NULL,
    template_id TEXT,
    -- Mirrors pricing_quotes_v2.code (the implementation requirement).
    code TEXT,
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


# Adds a nullable column to the existing narrator_tasks table so v2
# master-task rows can be joined to their snapshot. v1 rows keep
# `snapshot_id IS NULL` and behave exactly as before.
NARRATOR_TASKS_SNAPSHOT_LINK_SQL = """
ALTER TABLE narrator_tasks
    ADD COLUMN IF NOT EXISTS snapshot_id TEXT REFERENCES pricing_snapshots_v2 (snapshot_id);

CREATE INDEX IF NOT EXISTS narrator_tasks_snapshot_idx
    ON narrator_tasks (snapshot_id) WHERE snapshot_id IS NOT NULL;
"""


ALL_SCHEMA_SQL: tuple[str, ...] = (
    PRICING_QUOTES_V2_SCHEMA_SQL,
    PRICING_SNAPSHOTS_V2_SCHEMA_SQL,
    NARRATOR_TASKS_SNAPSHOT_LINK_SQL,
)


DOWNGRADE_SQL: tuple[str, ...] = (
    "DROP INDEX IF EXISTS narrator_tasks_snapshot_idx;",
    "ALTER TABLE narrator_tasks DROP COLUMN IF EXISTS snapshot_id;",
    "DROP INDEX IF EXISTS pricing_snapshots_v2_user_idx;",
    "DROP TABLE IF EXISTS pricing_snapshots_v2;",
    "DROP INDEX IF EXISTS pricing_quotes_v2_user_idx;",
    "DROP TABLE IF EXISTS pricing_quotes_v2;",
)
