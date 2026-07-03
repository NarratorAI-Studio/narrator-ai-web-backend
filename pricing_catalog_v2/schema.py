"""Raw SQL DDL strings for the v2 catalog tables.

Kept as constants so the Alembic migration and any future ad-hoc DB
inspection scripts read the same source of truth. SQLAlchemy Table
definitions in `db/tables.py` are the runtime model; this module is
the schema-creation surface.

PostgreSQL-specific syntax (TIMESTAMP WITH TIME ZONE, partial indexes
WHERE enabled = TRUE). The Alembic migration runs these against prod
Postgres; tests bypass this module entirely and bind SQLAlchemy
metadata to an in-memory SQLite engine.
"""

from __future__ import annotations


# The implementation requirement: code / name / learning_model_id are nullable upstream
# identity columns populated by
# `scripts/seed_pricing_template_v2_identity.py`. Mirrored by alembic
# revision `20260616_0001_pricing_template_v2_identity`. SQL kept
# comment-free inside the CREATE so SQLite's test-time parser doesn't
# reject the multi-line `--` block.
PRICING_TEMPLATE_V2_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pricing_template_v2 (
    template_id TEXT PRIMARY KEY,
    template_family_id TEXT,
    tier_multiplier NUMERIC(6, 4) NOT NULL DEFAULT 1.0 CHECK (tier_multiplier > 0),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    code TEXT,
    name TEXT,
    learning_model_id TEXT,
    video_duration_seconds INTEGER
);
"""


PRICING_CATALOG_V2_FAMILY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pricing_catalog_v2_family (
    template_family_id TEXT NOT NULL,
    tier_code TEXT NOT NULL,
    effective_version INTEGER NOT NULL CHECK (effective_version > 0),
    product_line TEXT NOT NULL,
    mode TEXT,
    quality TEXT,
    flash_pro_axis TEXT NOT NULL CHECK (flash_pro_axis IN ('required', 'optional')),
    manual_price INTEGER NOT NULL CHECK (manual_price >= 0),
    pro_surcharge_display INTEGER CHECK (pro_surcharge_display IS NULL OR pro_surcharge_display >= 0),
    system_reference_price INTEGER NOT NULL CHECK (system_reference_price >= 0),
    currency_unit TEXT NOT NULL DEFAULT 'web_point',
    raw_rate NUMERIC(10, 5) NOT NULL,
    final_rate NUMERIC(6, 2) NOT NULL,
    rounding_rule_version TEXT NOT NULL,
    manual_override_warning BOOLEAN NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by TEXT NOT NULL,
    PRIMARY KEY (template_family_id, tier_code, effective_version),
    -- Contract section 3.1 — when flash_pro_axis = 'optional'
    -- (derivative tier), mode + quality MUST be NULL so a future
    -- migration cannot half-fill a value.
    -- SQLite strictly requires table-constraints AFTER all column-defs.
    -- Postgres allows mixed placement, but we follow the strict order
    -- for cross-engine compatibility.
    CONSTRAINT flash_pro_axis_optional_nullifies_mode_quality_family
        CHECK (flash_pro_axis = 'required' OR (mode IS NULL AND quality IS NULL))
);

CREATE INDEX IF NOT EXISTS pricing_catalog_v2_family_current_idx
    ON pricing_catalog_v2_family (template_family_id, tier_code, effective_version DESC)
    WHERE enabled = TRUE;
"""


PRICING_CATALOG_V2_ENTRY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pricing_catalog_v2_entry (
    catalog_entry_id TEXT PRIMARY KEY,
    template_id TEXT NOT NULL,
    tier_code TEXT NOT NULL,
    effective_version INTEGER NOT NULL CHECK (effective_version > 0),
    product_line TEXT NOT NULL,
    mode TEXT,
    quality TEXT,
    flash_pro_axis TEXT NOT NULL CHECK (flash_pro_axis IN ('required', 'optional')),
    manual_price INTEGER NOT NULL CHECK (manual_price >= 0),
    pro_surcharge_display INTEGER CHECK (pro_surcharge_display IS NULL OR pro_surcharge_display >= 0),
    system_reference_price INTEGER NOT NULL CHECK (system_reference_price >= 0),
    currency_unit TEXT NOT NULL DEFAULT 'web_point',
    raw_rate NUMERIC(10, 5) NOT NULL,
    final_rate NUMERIC(6, 2) NOT NULL,
    rounding_rule_version TEXT NOT NULL,
    manual_override_warning BOOLEAN NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by TEXT NOT NULL,
    UNIQUE (template_id, tier_code, effective_version),
    -- Mirrors the family-side constraint, see schema.py family DDL.
    CONSTRAINT flash_pro_axis_optional_nullifies_mode_quality_entry
        CHECK (flash_pro_axis = 'required' OR (mode IS NULL AND quality IS NULL))
);

CREATE INDEX IF NOT EXISTS pricing_catalog_v2_entry_current_idx
    ON pricing_catalog_v2_entry (template_id, tier_code, effective_version DESC)
    WHERE enabled = TRUE;
"""


ALL_SCHEMA_SQL: tuple[str, ...] = (
    PRICING_TEMPLATE_V2_SCHEMA_SQL,
    PRICING_CATALOG_V2_FAMILY_SCHEMA_SQL,
    PRICING_CATALOG_V2_ENTRY_SCHEMA_SQL,
)


# Reverse order for the alembic downgrade path. Drops indexes first
# because some Postgres versions error on dropping a table that still
# has dependent indexes referenced in pg_depend.
DOWNGRADE_SQL: tuple[str, ...] = (
    "DROP INDEX IF EXISTS pricing_catalog_v2_entry_current_idx;",
    "DROP TABLE IF EXISTS pricing_catalog_v2_entry;",
    "DROP INDEX IF EXISTS pricing_catalog_v2_family_current_idx;",
    "DROP TABLE IF EXISTS pricing_catalog_v2_family;",
    "DROP TABLE IF EXISTS pricing_template_v2;",
)
