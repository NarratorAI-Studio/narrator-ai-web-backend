"""pricing_template_v2: add video_duration_seconds

Revision ID: 20260625_0001
Revises: 20260616_0001
Create Date: 2026-06-25

The implementation requirement. The 3-minute tiered pricing formula needs each template's
real video duration as input. Upstream `/v2/res/movie-baokuan` returns
it as a `MM:SS` string in `time`; we persist it locally as integer
seconds so quote time + the system_reference_price refresh script can
both read it without a round-trip to upstream.

Nullable + IF NOT EXISTS — additive, matches the regression coverage precedent. Values
are populated by `scripts/refresh_v2_system_reference_price.py`.
"""

from __future__ import annotations

from alembic import op


revision = "20260625_0001"
down_revision = "20260616_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE pricing_template_v2 "
        "ADD COLUMN IF NOT EXISTS video_duration_seconds INTEGER"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE pricing_template_v2 DROP COLUMN video_duration_seconds"
    )
