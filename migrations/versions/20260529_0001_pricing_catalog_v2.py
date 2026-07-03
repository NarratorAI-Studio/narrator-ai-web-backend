"""create pricing_catalog_v2 tables for template price v2

Revision ID: 20260529_0001
Revises: 20260527_0001
Create Date: 2026-05-29

Implements the schema portion of the template price v2 catalog (the implementation requirement).
Three additive tables; v1's `fa_template_price` stays in place for
HP-XX quote/pin compatibility.

The schema mirrors `docs/pricing/v2/catalog-tier-contract.md` (frozen
2026-05-29 in narrator-ai-web review). Tier list (not 5 hardcoded
columns), `template_family` × `tier_multiplier` inheritance, raw_rate
/ final_rate / rounding_rule_version triple per row, derivative-tier
`flash_pro_axis: optional`.
"""

from __future__ import annotations

from alembic import op

from pricing_catalog_v2.schema import ALL_SCHEMA_SQL, DOWNGRADE_SQL


revision = "20260529_0001"
down_revision = "20260527_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for sql in ALL_SCHEMA_SQL:
        op.execute(sql)


def downgrade() -> None:
    for sql in DOWNGRADE_SQL:
        op.execute(sql)
