"""merge cloud_drive and pricing_quote_v2 heads

The cloud-drive PR  branched off 20260527_0001 (narrator_tasks)
and added 20260528_0001 (user_cloud_files); meanwhile template price v2
on main branched off the same parent and added 20260529_0001
(pricing_catalog_v2) -> 20260601_0001 (pricing_quote_snapshot_v2).
After review merge there were two alembic heads, which would block
`alembic upgrade head` on deploy. This is an empty merge — the two
branches touch disjoint tables, so no schema operation is needed.

Revision ID: 20260601_0002
Revises: 20260528_0001, 20260601_0001
Create Date: 2026-06-01 03:18:59.560039+00:00
"""

from __future__ import annotations


revision = "20260601_0002"
down_revision = ("20260528_0001", "20260601_0001")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
