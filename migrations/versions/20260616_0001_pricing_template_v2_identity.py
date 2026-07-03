"""pricing_template_v2: add code / name / learning_model_id

Revision ID: 20260616_0001
Revises: 20260611_0001
Create Date: 2026-06-16

The implementation requirement. Surface the upstream template identity (xy-code, human
name, narrator learning_model_id) alongside the local `template_id`
so the admin pricing-catalog UI can show "what is template 46?"
without a side trip to res_movie_baokuan. Columns are nullable so
the migration is additive — values arrive via the seeder script
`scripts/seed_pricing_template_v2_identity.py`, not by user input.

Upstream `/v2/res/movie-baokuan` does not return `code` at runtime
(see `project_movie_baokuan_upstream_code_field` memory), so the
authoritative source is the operations CSV dump.

IF NOT EXISTS for fresh-DB / DR consistency, matching the precedent
set by the migration fixes in regression coverage.
"""

from __future__ import annotations

from alembic import op


revision = "20260616_0001"
down_revision = "20260611_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE pricing_template_v2 ADD COLUMN IF NOT EXISTS code TEXT"
    )
    op.execute(
        "ALTER TABLE pricing_template_v2 ADD COLUMN IF NOT EXISTS name TEXT"
    )
    op.execute(
        "ALTER TABLE pricing_template_v2 "
        "ADD COLUMN IF NOT EXISTS learning_model_id TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE pricing_template_v2 DROP COLUMN learning_model_id")
    op.execute("ALTER TABLE pricing_template_v2 DROP COLUMN name")
    op.execute("ALTER TABLE pricing_template_v2 DROP COLUMN code")
