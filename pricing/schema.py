from __future__ import annotations


TEMPLATE_PRICE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fa_template_price (
    template_id INTEGER NOT NULL,
    combo_key TEXT NOT NULL,
    hard_price NUMERIC(18, 2) NOT NULL CHECK (hard_price > 0),
    text_chars INTEGER,
    text_lines INTEGER,
    pricing_rule_version INTEGER NOT NULL CHECK (pricing_rule_version > 0),
    is_current BOOLEAN NOT NULL,
    source_sheet_id TEXT,
    PRIMARY KEY (template_id, combo_key, pricing_rule_version)
);

CREATE INDEX IF NOT EXISTS fa_template_price_current_idx
    ON fa_template_price (template_id, combo_key, is_current);

CREATE UNIQUE INDEX IF NOT EXISTS fa_template_price_current_unique
    ON fa_template_price (template_id, combo_key)
    WHERE is_current;

CREATE INDEX IF NOT EXISTS fa_template_price_source_sheet_id_idx
    ON fa_template_price (source_sheet_id);
"""
